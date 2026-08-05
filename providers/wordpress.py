"""WordPress provider implementation.

Publishes to a self-hosted WordPress site through the built-in REST API
(``/wp-json/wp/v2``), authenticating with an APPLICATION PASSWORD — the
credential WordPress 5.6+ issues per user under Users → Profile, revocable
on its own without touching the account's real password.

Three values are needed rather than one, so unlike DEV.to the token is a
pair: the site lives in the account's ``instance_url`` (the same field
Mastodon and Bluesky use), and ``username:application_password`` is stored
as the access token and sent as HTTP Basic. That is what the WordPress
REST API expects and it keeps the credential in the one encrypted column
that already exists.

WORDPRESS UPLOADS BYTES. Instagram, Threads, Facebook and Google Business
are handed a URL and fetch it themselves, which is why they need a
publicly reachable media host; WordPress takes the file in the request
body. A private deployment can therefore publish to its own website with
no public media host at all.

Docs: https://developer.wordpress.org/rest-api/reference/posts/
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from urllib.parse import urlsplit

from .base import SocialProvider
from .exceptions import PublishError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

#: WordPress caps nothing in particular on post length; this is a sane
#: ceiling so the composer shows a limit rather than nothing at all.
MAX_BODY_LENGTH = 65535
#: Titles are a single DB column and long ones break every theme's layout.
MAX_TITLE_LENGTH = 200


class WordPressProvider(SocialProvider):
    """WordPress REST API provider using an application password."""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "WordPress"

    @property
    def auth_type(self) -> AuthType:
        # Application password; the credential IS the token, as with
        # Bluesky and DEV.to. No app registration, so nothing to configure
        # at deployment level.
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return MAX_BODY_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.ARTICLE, PostType.TEXT, PostType.IMAGE, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # It is the club's own server. The only real limit is politeness.
        return RateLimitConfig(requests_per_hour=1000, requests_per_day=10000, publish_per_day=200)

    # ------------------------------------------------------------------
    # OAuth stubs — not applicable
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        raise NotImplementedError(
            "WordPress uses an application password, not OAuth. Use connect_wordpress instead."
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        raise NotImplementedError(
            "WordPress uses an application password, not OAuth. Use connect_wordpress instead."
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Validate the credential and return the authenticated WP user.

        ``context=edit`` is requested deliberately: it is refused unless the
        credential really can edit, so this doubles as a permission check at
        connect time rather than at the first failed publish.
        """
        resp = self._request(
            "GET",
            f"{self._api_base()}/users/me",
            headers=self._auth_headers(access_token),
            params={"context": "edit"},
        )
        data = resp.json()
        avatars = data.get("avatar_urls") or {}
        return AccountProfile(
            platform_id=str(data.get("id", "")),
            name=data.get("name") or data.get("slug", ""),
            handle=data.get("slug"),
            # WordPress hands back a dict keyed by pixel size as a string.
            avatar_url=avatars.get("96") or avatars.get("48") or None,
            follower_count=0,
            extra={"site_url": self._site_url(), "roles": data.get("roles", [])},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Create a published post, uploading any media to the library first."""
        title = (content.title or "").strip()
        if not title:
            raise PublishError(
                "WordPress requires a title. Set the post title before publishing.",
                platform=self.platform_name,
            )

        # Upload first, so a failure here happens BEFORE a post exists and
        # cannot leave a published article with missing pictures.
        uploaded = [self._upload_media(access_token, path) for path in (content.media_files or [])]

        body = self._render_body(content, uploaded)

        payload: dict = {
            "title": title[:MAX_TITLE_LENGTH],
            "content": body,
            "status": "publish",
        }
        # The first image becomes the featured image, which is what themes
        # use for listings and social cards. It is still rendered in the
        # body by _render_body only when there is more than one.
        if uploaded:
            payload["featured_media"] = uploaded[0]["id"]

        resp = self._request(
            "POST",
            f"{self._api_base()}/posts",
            headers=self._auth_headers(access_token),
            json=payload,
        )
        data = resp.json()
        post_id = data.get("id")
        if not post_id:
            raise PublishError(
                f"WordPress post creation returned no id: {data}",
                platform=self.platform_name,
                raw_response=data,
            )
        return PublishResult(
            platform_post_id=str(post_id),
            url=data.get("link"),
            extra=data,
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Post a comment on a published article (the 'first comment' slot)."""
        resp = self._request(
            "POST",
            f"{self._api_base()}/comments",
            headers=self._auth_headers(access_token),
            json={"post": int(post_id), "content": text},
        )
        data = resp.json()
        comment_id = data.get("id")
        if not comment_id:
            raise PublishError(
                f"WordPress comment creation returned no id: {data}",
                platform=self.platform_name,
                raw_response=data,
            )
        return CommentResult(platform_comment_id=str(comment_id), extra=data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _site_url(self) -> str:
        """The site root, without a trailing slash.

        Accepts ``site_url`` (what the engine injects from the account's
        ``instance_url``) or ``instance_url`` directly, so the provider can
        be constructed either way in tests.
        """
        raw = self.credentials.get("site_url") or self.credentials.get("instance_url") or ""
        return str(raw).rstrip("/")

    def _api_base(self) -> str:
        site = self._site_url()
        if not site:
            raise PublishError(
                "No WordPress site URL. Reconnect the account.",
                platform=self.platform_name,
            )
        return f"{site}/wp-json/wp/v2"

    @staticmethod
    def _auth_headers(access_token: str) -> dict:
        """HTTP Basic from a stored ``username:application_password``.

        WordPress displays application passwords in groups of four with
        spaces. It accepts them either way, and people paste them either
        way, so nothing here strips them - the credential is sent exactly
        as it was stored.
        """
        encoded = base64.b64encode(access_token.encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        }

    def _upload_media(self, access_token: str, path: str) -> dict:
        """Upload one local file to the media library, returning its record.

        Sends the bytes in the request body with a Content-Disposition
        filename, which is what /wp/v2/media expects.
        """
        filename = os.path.basename(path)
        mime, _ = mimetypes.guess_type(filename)
        with open(path, "rb") as handle:
            payload = handle.read()

        headers = {
            **self._auth_headers(access_token),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mime or "application/octet-stream",
        }
        resp = self._request(
            "POST",
            f"{self._api_base()}/media",
            headers=headers,
            data=payload,
        )
        data = resp.json()
        if not data.get("id"):
            raise PublishError(
                f"WordPress media upload returned no id: {data}",
                platform=self.platform_name,
                raw_response=data,
            )
        return data

    def _render_body(self, content: PublishContent, uploaded: list[dict]) -> str:
        """Build the post body: the caption, then any extra images.

        The FIRST upload is the featured image and is deliberately not
        repeated here - themes render it above the article already, and
        emitting it twice is the commonest way a WordPress post looks
        wrong. Everything after the first is embedded, because otherwise it
        would sit in the media library unreferenced.
        """
        parts: list[str] = []
        text = (content.description or content.text or "").strip()
        if text:
            parts.append(text)

        for item in uploaded[1:]:
            source = item.get("source_url")
            if not source:
                continue
            alt = (item.get("alt_text") or "").replace('"', "&quot;")
            parts.append(f'<figure class="wp-block-image"><img src="{source}" alt="{alt}" /></figure>')

        if content.link_url:
            parts.append(f'<p><a href="{content.link_url}">{content.link_url}</a></p>')

        return "\n\n".join(parts)[:MAX_BODY_LENGTH]

    @staticmethod
    def normalise_site_url(raw: str) -> str:
        """Normalise user input to ``scheme://host[:port][/path]``.

        Accepts ``example.org``, ``https://example.org/``, or a URL with a
        subdirectory install path. Defaults the scheme to https. Returns an
        empty string when there is no host, so the caller can refuse.
        """
        value = (raw or "").strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"https://{value}"
        parts = urlsplit(value)
        if not parts.netloc:
            return ""
        scheme = parts.scheme or "https"
        path = parts.path.rstrip("/")
        return f"{scheme}://{parts.netloc}{path}"
