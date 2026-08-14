"""Impressionen — the club's own picture wall. PUBLISHES NOWHERE.

WHAT THIS IS FOR. The club's website has an *Impressionen* page: every
picture the club has published, on its own wall. Until now the only way
onto that wall was to publish a post to Instagram, because
brightbean-surface builds the gallery from PlatformPosts whose status is
``published`` on the configured accounts. So a photograph the club wanted
on its own website but NOT on Instagram had nowhere to go.

This is that somewhere. It is a channel like any other — it appears in
the composer, it takes media, it can be scheduled — and it has no
endpoint behind it at all.

THE MECHANISM IS THE STATUS, AND NOTHING ELSE. ``publish_post`` performs
no network call and returns immediately, so the engine marks the
PlatformPost ``published`` (see apps/publisher/engine.py), and
``published`` is exactly what the website matches. Nothing is sent
anywhere because there is nowhere to send it: this provider has no API
base, no token exchange and no HTTP call of any kind. That is a stronger
guarantee than a flag somebody could flip — you cannot post to a
platform that has no address.

IT NEEDS NO CREDENTIALS, at either level. There is no app to register,
so it has no entry in ``REQUIRED_CREDENTIAL_KEYS``; and there is no
account to log into, so connecting is one press with nothing to type.
``AuthType.SESSION`` is what makes ``_get_configured_platforms`` treat it
as configured without an app credential, exactly as Bluesky and Mastodon
are treated.

WHY IT REFUSES A POST WITH NO PICTURE. The website's gallery query ends

    AND ma.media_type IN ('image', 'gif')

so a text-only post — or a video-only one — would publish perfectly
successfully and then appear NOWHERE. That is the failure this codebase
keeps writing comments about: the one that looks like success. Better to
refuse at the moment of publishing, where the composer shows the error
against the post, than to leave somebody wondering why their picture
never arrived.
"""

from __future__ import annotations

import logging
import uuid

from .base import SocialProvider
from .exceptions import PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

#: What a channel is called when nobody says otherwise.
DEFAULT_NAME = "Impressionen"

#: The account's id on "the platform", DERIVED FROM ITS NAME.
#:
#: It was a constant, and the constant was the guarantee: SocialAccount
#: is unique on (workspace, platform, account_platform_id), so one fixed
#: value meant exactly one channel per workspace. Naming is free now, so
#: the NAME carries that job instead - the same name is the same channel
#: and pressing connect twice updates it rather than growing a second one
#: nobody can tell apart, while a different name is a different channel.
#:
#: SLUGGED RATHER THAN USED RAW, because this is an identity: "Bilder
#: 2026" and "bilder 2026" are the same channel to a person and would be
#: two rows to a database. Lowercase, and anything that is not a letter
#: or a digit becomes a single dash.
#:
#: NOTE FOR ANYONE ADDING A SECOND CHANNEL HERE: the club's website pins
#: its Impressionen wall to this PLATFORM and not to an account - see
#: surface_gallery_extra_platforms - which was safe only while one
#: channel per workspace was structurally guaranteed. It no longer is.
#: EVERY channel on this platform now shows on that public wall. A
#: staging area that must NOT be public is a different platform, not a
#: different name.
def channel_id(name: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in (name or ""))
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "impressionen"

#: Stored as the account's token. NOT a secret and not a credential -
#: there is nothing to authenticate against. It exists because several
#: code paths test ``if account.oauth_access_token:`` to decide whether an
#: account is worth acting on, and an empty string there reads as "never
#: connected" rather than "needs nothing".
PLACEHOLDER_TOKEN = "impressionen-local"

#: What the website's gallery will actually show. Kept beside the check
#: that uses it so the two cannot drift: brightbean-surface filters on
#: ``media_type IN ('image', 'gif')``.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class ImpressionenProvider(SocialProvider):
    """The club's own picture wall. Every method here is deliberately inert."""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Impressionen"

    @property
    def auth_type(self) -> AuthType:
        # There is nothing to authenticate. SESSION is what tells
        # _get_configured_platforms this platform needs no app-level
        # credentials, which is the same answer Bluesky and Mastodon give
        # for their own reasons.
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        # The wall shows pictures, not captions - but a caption is still
        # worth keeping with the post, and the alt text the gallery
        # renders comes from the attachment rather than from here.
        return 2200

    @property
    def supported_post_types(self) -> list[PostType]:
        # NO TEXT. A text-only post would publish successfully and show
        # up nowhere; see the module docstring. Declaring the truth here
        # is what lets the composer stop somebody earlier than publish.
        return [PostType.IMAGE, PostType.CAROUSEL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        # Stills only, matching what the gallery renders. A video in a
        # photo wall is a black rectangle.
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Nothing is called, so nothing can be throttled. The numbers are
        # a formality; a limit of zero would be read as "blocked".
        return RateLimitConfig(
            requests_per_hour=100000,
            requests_per_day=100000,
            publish_per_day=100000,
        )

    # ------------------------------------------------------------------
    # OAuth stubs - there is no OAuth, and there is no anything else
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        raise NotImplementedError(
            "Impressionen is the club's own wall - there is nothing to log in to. "
            "Use connect_impressionen."
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        raise NotImplementedError(
            "Impressionen is the club's own wall - there is nothing to log in to. "
            "Use connect_impressionen."
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """The channel, described without asking anybody.

        THE ARGUMENT IS THE CHANNEL'S NAME, not a credential, and that
        is not a trick played on the signature - it is the only honest
        reading. There is no remote account to fetch and no secret to
        present, so the base class's ``access_token`` has nothing to
        carry here. What the connect screen collects is a name, and this
        is the one place a channel's identity is decided.

        A BLANK OR PLACEHOLDER ARGUMENT MEANS THE DEFAULT, which is what
        keeps ``validate_token`` honest: it calls this with the stored
        token, and a health check on a channel that reaches no network
        must never be able to fail.
        """
        name = (access_token or "").strip()
        if not name or name == PLACEHOLDER_TOKEN:
            name = DEFAULT_NAME
        return AccountProfile(
            platform_id=channel_id(name),
            name=name[:255],
            handle=channel_id(name),
            avatar_url="",
            follower_count=0,
        )

    # ------------------------------------------------------------------
    # Publishing - the whole point, and it does nothing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Accept the post and send it precisely nowhere.

        Returning a PublishResult is what makes the engine write
        ``status = PUBLISHED``, and that status is the entire mechanism:
        brightbean-surface reads published PlatformPosts on the
        configured accounts to build the wall. So this "publish" is a
        publication to the club's own website and to nothing else.

        THE REFUSAL BELOW IS THE ONLY THING THAT CAN FAIL HERE, and it
        earns its place: without it a post with no picture would report
        success and appear on no page at all.
        """
        pictures = self._pictures(content)
        if not pictures:
            raise PublishError(
                "Impressionen shows pictures, and this post has none. Attach "
                "at least one image (jpg, png, gif or webp) - a video or a "
                "text-only post would publish successfully and then appear "
                "nowhere on the wall.",
                platform=self.platform_name,
            )

        logger.info(
            "Impressionen: accepting %d picture(s) for the club's own wall; "
            "nothing is sent anywhere",
            len(pictures),
        )

        # A local identifier so the row has one. It refers to nothing
        # remote, because there is nothing remote - and it is generated
        # rather than derived from the post so that re-publishing cannot
        # silently collide with an earlier row.
        return PublishResult(
            platform_post_id=uuid.uuid4().hex,
            # No url: the wall shows every published picture together and
            # there is no per-post page to link to. Inventing one would be
            # a link that 404s.
            url=None,
            extra={"pictures": len(pictures), "sent_to": "nowhere"},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _pictures(cls, content: PublishContent) -> list[str]:
        """The attachments the gallery would actually render.

        Matched on the suffix, with any query string dropped first -
        stored media arrives as a presigned or absolute URL and
        ``?X-Amz-...`` on the end would defeat a naive endswith. The same
        approach devto.py takes for its cover image.
        """
        found: list[str] = []
        for url in list(content.media_urls or []) + list(content.media_files or []):
            path = str(url).split("?", 1)[0].lower()
            if path.endswith(IMAGE_SUFFIXES):
                found.append(url)
        return found
