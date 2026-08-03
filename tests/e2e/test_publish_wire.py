"""End-to-end: each supported platform puts the RIGHT BYTES on the wire.

Rule 8 - every supported platform must be tested end to end where possible,
preferring official testing aids, then community ones, and falling back to
tests written from the official API documentation.

WHY THIS IS SEPARATE FROM test_publish_path.py. That module proves the whole
chain for one platform in depth: the schedule fires, the engine claims, media
is downloaded and cleaned up, retries land, the first comment defers. This one
proves a SINGLE property for MANY platforms - that the provider builds the
request the platform's API actually documents - and is shaped so that adding a
platform is adding a case, not a file.

WHAT IS REAL AND WHAT IS NOT. Real: the production command, the schedule, the
engine, credential resolution, and the provider's own request building and
response parsing. Faked: the wire, and only the wire, via httpx's MockTransport.

The seam is at the wire on purpose. A test that patches
PublishEngine._dispatch_to_provider never runs the provider at all, so a
malformed payload passes unnoticed - which is why deleting the facet parsing
from the Bluesky provider left every status-level test green.

transaction=True is REQUIRED, not incidental: poll_and_publish fans out over a
ThreadPoolExecutor whose threads open their own connections, so under the
default wrapping they would see an empty database and publish nothing - the
test would pass while proving nothing.
"""

import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

CAPTION = "shipped by the worker"


class WireRecorder:
    """Answers the calls a provider makes, and keeps every request it made.

    `routes` maps a URL-path SUFFIX to a handler taking the request and
    returning an httpx.Response. Anything unrouted is recorded as unexpected
    and answered 404 rather than quietly satisfied, so a provider reaching for
    an endpoint this test did not predict SHOWS UP instead of passing silently.
    """

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, responder in self.routes.items():
            if request.url.path.endswith(suffix):
                return responder(request)
        self.unexpected.append(f"{request.method} {request.url.path}")
        return httpx.Response(404, json={"error": "unrouted in this test"})

    def call(self, suffix: str) -> httpx.Request | None:
        for request in self.requests:
            if request.url.path.endswith(suffix):
                return request
        return None

    def form(self, suffix: str) -> dict:
        """The form body of the first request to `suffix`, as a flat dict."""
        request = self.call(suffix)
        assert request is not None, f"the provider never called {suffix}"
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def wire(monkeypatch):
    """Install a WireRecorder over every httpx.Client this process builds.

    providers/base.py constructs `httpx.Client(timeout=...)` inline per request,
    so there is no transport to inject - the class itself is replaced. That is
    deliberately process-wide, because the engine's worker threads build their
    own clients.
    """
    real_client = httpx.Client

    def install(routes) -> WireRecorder:
        recorder = WireRecorder(routes)

        def client_with_mock_transport(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recorder.handle)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client_with_mock_transport)
        return recorder

    return install


#: The worker has no --once; `process_tasks` loops until killed, so a duration
#: is the only way to get it to return.
WORKER_SECONDS = "5"


def run_the_worker():
    """Enqueue the publish cycle and run the REAL worker until it drains.

    Enqueued explicitly rather than waited for. `post_migrate` registers the
    cycle at repeat=15, so a test COULD sit and hope the recurring row comes
    due inside the worker's lifetime - which makes every assertion below depend
    on wall-clock timing against a fifteen-second cadence, and that is how a
    suite becomes intermittently red for reasons nobody can reproduce.

    Calling a @background function IS the enqueue, so this is the same row the
    scheduler would have written; only its arrival is made deterministic.
    """
    from apps.publisher.tasks import run_publish_cycle

    run_publish_cycle()
    call_command("process_tasks", "--duration", WORKER_SECONDS)


# THE PARAMETER ORDER HERE IS LOAD-BEARING, which is not a sentence anybody
# wants to write about a test helper.
#
# With `instance_url=""` sitting immediately after `token`, gitleaks'
# generic-api-key rule reads the two as an assignment: keyword `token`,
# separator `,`, then `instance_url=` as a 13-character candidate value,
# terminated by the quote of the empty default. It reports a secret at this
# line and fails the repository's secret scan. There is no secret here - every
# credential in this module is a visible literal like "mastodon-access-token".
#
# Keeping the defaulted parameters away from `token` removes the pattern. If
# you tidy this back into a more natural order, re-run the scanner first,
# because CI will not forgive it:
#
#     gitleaks detect --source . --redact --verbose --no-banner
def schedule_a_post(*, platform, platform_id, token, caption=CAPTION, instance_url="", title="", platform_extra=None):
    """One account on `platform` with one post due a minute ago.

    `title` is only meaningful for the platforms whose API demands one - DEV.to
    refuses an article without it, YouTube and Pinterest carry it as a separate
    field - so it defaults to empty and most cases never pass it.

    `platform_extra` is the composer's per-platform metadata, which the engine
    merges into PublishContent.extra. Several providers REFUSE to publish
    without a particular key in it - Pinterest wants `board_id`, LinkedIn's
    company variant an `author` URN - so a case for those platforms is not
    expressible without it.
    """
    organization = Organization.objects.create(name=f"E2E {platform} Org")
    workspace = Workspace.objects.create(name=f"E2E {platform} WS", organization=organization)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id,
        account_name=f"Brightbean {platform}",
        oauth_access_token=token,
        instance_url=instance_url,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    post = Post.objects.create(workspace=workspace, caption=caption, title=title)
    return PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
        platform_extra=platform_extra or {},
    )


def attach_an_image(post, *, filename="e2e.png"):
    """Attach one image to `post`, the way the composer does, and return the asset.

    A URL string will not do. Several platforms refuse a post carrying no media
    at all - Pinterest and Instagram among them - and the URL those providers
    put on the wire is not something a test hands them: the engine reads
    `asset.file.url` off the storage backend and makes it absolute against
    APP_URL. Going through a real MediaAsset is what puts that step under test
    rather than around it.

    The bytes are plain ASCII and nothing reads them - these providers forward
    a URL, and the one that uploads bytes (Bluesky) is covered in
    test_publish_path.py.
    """
    workspace = post.workspace
    asset = MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        filename=filename,
        media_type="image",
        mime_type="image/png",
    )
    asset.file.save(filename, ContentFile(b"pretend this is a png" * 4), save=True)
    PostMedia.objects.create(post=post, media_asset=asset, position=0)
    return asset


def attach_a_video(post, *, filename="e2e.mp4", duration_sec=12.0):
    """Attach one video to `post`, and return the asset.

    An image will NOT substitute for the video-only platforms. The engine
    discards every non-video attachment when the provider's supported_post_types
    are a subset of VIDEO/SHORT, so attaching an image to a TikTok or YouTube
    post leaves it with NO media at all - and both providers then refuse it
    outright, which looks like a provider bug rather than a fixture mistake.

    `duration_sec` matters: the engine passes the first video's duration to the
    provider as video_duration_sec, and TikTok compares it against the
    creator's own max_video_post_duration_sec BEFORE uploading anything.
    """
    workspace = post.workspace
    asset = MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        filename=filename,
        media_type="video",
        mime_type="video/mp4",
        duration=duration_sec,
    )
    asset.file.save(filename, ContentFile(b"pretend this is an mp4" * 8), save=True)
    PostMedia.objects.create(post=post, media_asset=asset, position=0)
    return asset


def assert_published(platform_post, expected_id):
    platform_post.refresh_from_db()
    assert platform_post.status == PlatformPost.Status.PUBLISHED, (
        f"still {platform_post.status!r}; publish_error={platform_post.publish_error!r}"
    )
    assert platform_post.platform_post_id == expected_id
    assert platform_post.published_at is not None


# ---------------------------------------------------------------------------
# Mastodon - https://docs.joinmastodon.org/methods/statuses/#create
# ---------------------------------------------------------------------------

MASTODON_INSTANCE = "https://mastodon.example"
MASTODON_STATUS_ID = "109999999999999999"
MASTODON_TOKEN = "mastodon-access-token"


@pytest.mark.django_db(transaction=True)
def test_mastodon_posts_a_status_to_its_own_instance(wire, monkeypatch):
    """A Mastodon status is a form POST to the ACCOUNT'S OWN instance.

    Two things here are specific to Mastodon, and are what this test is for.

    The host is PER ACCOUNT, not a constant: the engine copies
    `account.instance_url` into the credentials and the provider builds every
    URL from it. A bug there publishes to the wrong server - or, if the value
    is dropped, to a relative URL that never leaves the process.

    And the body is FORM-encoded, not JSON. The API documents
    application/x-www-form-urlencoded for POST /api/v1/statuses, so asserting
    on parsed form fields is asserting the documented contract.

    `is_safe_url` is patched because it calls socket.getaddrinfo - a real DNS
    lookup. It is our own SSRF validator and has its own tests; leaving it live
    would make this a network-dependent test of somebody else's nameserver.
    """
    monkeypatch.setattr("apps.common.validators.is_safe_url", lambda url: True)

    recorder = wire(
        {
            "/api/v1/statuses": lambda request: httpx.Response(
                200,
                json={
                    "id": MASTODON_STATUS_ID,
                    "url": f"{MASTODON_INSTANCE}/@brightbean/{MASTODON_STATUS_ID}",
                    "content": CAPTION,
                },
            ),
            # NOT publishing, and routed anyway. The worker runs the WHOLE
            # recurring schedule, not just the publish cycle, so this account
            # also gets an inbox poll and an OAuth health check inside the same
            # window. Leaving them unrouted makes the recorder report them as
            # unpredicted and fails a test about publishing for a reason that
            # has nothing to do with publishing.
            "/api/v1/notifications": lambda request: httpx.Response(200, json=[]),
            "/api/v1/accounts/verify_credentials": lambda request: httpx.Response(
                200,
                json={
                    "id": "110000000000000001",
                    "username": "brightbean",
                    "acct": "brightbean",
                    "display_name": "Brightbean",
                    "followers_count": 7,
                },
            ),
        }
    )
    platform_post = schedule_a_post(
        platform="mastodon",
        platform_id="110000000000000001",
        token=MASTODON_TOKEN,
        instance_url=MASTODON_INSTANCE,
    )

    run_the_worker()

    assert recorder.unexpected == [], f"unpredicted API calls: {recorder.unexpected}"
    assert_published(platform_post, MASTODON_STATUS_ID)

    request = recorder.call("/api/v1/statuses")
    assert str(request.url).startswith(MASTODON_INSTANCE), (
        f"published to {request.url}, not to the account's own instance - the "
        f"per-account instance_url did not reach the provider"
    )
    assert request.headers["Authorization"] == f"Bearer {MASTODON_TOKEN}"

    body = recorder.form("/api/v1/statuses")
    assert body["status"] == CAPTION
    # The API defaults visibility to the account's own setting when the field
    # is absent, so sending it explicitly is what makes the outcome predictable.
    assert body["visibility"] == "public"


# ---------------------------------------------------------------------------
# DEV.to - https://developers.forem.com/api/v1#tag/articles/operation/createArticle
# ---------------------------------------------------------------------------

DEVTO_ARTICLE_ID = 1234567
DEVTO_KEY = "devto-personal-api-key"
DEVTO_TITLE = "Shipping from Brightbean"
DEVTO_CAPTION = f"{CAPTION} #brightbean"


@pytest.mark.django_db(transaction=True)
def test_devto_publishes_an_article_authenticated_by_its_api_key_header(wire):
    """A DEV.to article is a JSON POST authenticated by the `api-key` HEADER.

    Forem has no OAuth flow and does not accept a bearer token: every
    authenticated endpoint is reached with an `api-key` header
    (https://developers.forem.com/api). The account's stored token IS that key.
    Sending it the way every other provider here sends one - as
    `Authorization: Bearer` - answers 401, and no status-level assertion can
    tell the two apart, because both are "the provider sent the token".

    Three more things this platform does not share with any other case here:
    the body is JSON under an `article` envelope; `title` is MANDATORY, so it
    has to travel from Post.title through effective_title to reach the wire;
    and `published` decides whether the article is live or a draft.
    """
    recorder = wire(
        {
            "/api/articles": lambda request: httpx.Response(
                201,
                json={
                    "id": DEVTO_ARTICLE_ID,
                    "title": DEVTO_TITLE,
                    "url": f"https://dev.to/brightbean/{DEVTO_ARTICLE_ID}",
                },
            ),
            # NOT publishing, and routed anyway - the health check runs inside
            # the same worker window and calls get_profile. See the Mastodon
            # case for why leaving it unrouted fails a publishing test for a
            # reason that has nothing to do with publishing.
            "/api/users/me": lambda request: httpx.Response(
                200,
                json={"id": 99, "username": "brightbean", "name": "Brightbean"},
            ),
        }
    )
    platform_post = schedule_a_post(
        platform="devto",
        platform_id="99",
        token=DEVTO_KEY,
        caption=DEVTO_CAPTION,
        title=DEVTO_TITLE,
    )

    run_the_worker()

    assert recorder.unexpected == [], f"unpredicted API calls: {recorder.unexpected}"
    assert_published(platform_post, str(DEVTO_ARTICLE_ID))

    request = recorder.call("/api/articles")
    assert request.headers["api-key"] == DEVTO_KEY
    assert "Authorization" not in request.headers, (
        "the key went out as a bearer token; Forem authenticates on the api-key header and answers 401 to anything else"
    )
    # Forem versions its API by Accept header rather than by URL.
    assert request.headers["accept"] == "application/vnd.forem.api-v1+json"

    article = json.loads(request.content)["article"]
    assert article["title"] == DEVTO_TITLE
    assert article["body_markdown"] == DEVTO_CAPTION
    # Omitted, Forem creates a DRAFT - and nothing downstream would notice,
    # because the row goes PUBLISHED here either way while the article sits
    # unpublished on the platform.
    assert article["published"] is True
    # The provider parses #hashtags out of the caption into Forem's tag list,
    # lowercased and stripped of everything that is not alphanumeric.
    assert article["tags"] == ["brightbean"]


# ---------------------------------------------------------------------------
# Facebook - https://developers.facebook.com/docs/graph-api/reference/page/feed/#publish
# ---------------------------------------------------------------------------

FACEBOOK_PAGE_ID = "111222333444555"
FACEBOOK_POST_ID = "987654321"
FACEBOOK_TOKEN = "facebook-page-access"


@pytest.mark.django_db(transaction=True)
def test_facebook_posts_to_the_connected_pages_feed(wire):
    """A Facebook text post is a JSON POST to /{page-id}/feed.

    THE PAGE ID IS NOT A CONSTANT, and that is what this case is really for.
    The provider raises without one, and the engine supplies it from the
    CONNECTED ACCOUNT's own account_platform_id. Asserting the URL is
    therefore asserting that the page the user connected is the page that gets
    posted to - publishing somebody else's page is not a thing any status
    check can see, because the row goes PUBLISHED either way.

    Graph answers with a PAGE-SCOPED id, `{page-id}_{post-id}`, and the
    provider stores only the post half. Everything that later reads
    platform_post_id - post analytics, the first comment - is built on which
    half landed in the row.
    """
    recorder = wire(
        {
            f"/{FACEBOOK_PAGE_ID}/feed": lambda request: httpx.Response(
                200,
                json={"id": f"{FACEBOOK_PAGE_ID}_{FACEBOOK_POST_ID}"},
            ),
            # NOT publishing, and routed anyway - the health check, the inbox
            # poll and the ANALYTICS sync all run in the same worker window.
            # See the Mastodon case for why leaving them unrouted fails a
            # publishing test for reasons unrelated to publishing.
            #
            # Facebook is by far the noisiest account here, and the shape of
            # the noise is itself worth knowing: page insights are fetched ONE
            # METRIC PER REQUEST (fetch_insights_safe, so that one unsupported
            # metric cannot fail the rest), and post insights are attempted
            # against BOTH the bare and the page-scoped id. An unrouted run of
            # this case reports 42 such calls.
            "/insights": lambda request: httpx.Response(200, json={"data": []}),
            "/v25.0/me": lambda request: httpx.Response(
                200,
                json={"id": FACEBOOK_PAGE_ID, "name": "Brightbean", "picture": {"data": {"url": ""}}},
            ),
            "/conversations": lambda request: httpx.Response(200, json={"data": []}),
            f"/v25.0/{FACEBOOK_PAGE_ID}": lambda request: httpx.Response(200, json={"followers_count": 7}),
            f"/v25.0/{FACEBOOK_POST_ID}": lambda request: httpx.Response(200, json={"id": FACEBOOK_POST_ID}),
            f"/v25.0/{FACEBOOK_PAGE_ID}_{FACEBOOK_POST_ID}": lambda request: httpx.Response(
                200,
                json={"id": f"{FACEBOOK_PAGE_ID}_{FACEBOOK_POST_ID}"},
            ),
        }
    )
    platform_post = schedule_a_post(
        platform="facebook",
        platform_id=FACEBOOK_PAGE_ID,
        token=FACEBOOK_TOKEN,
    )

    run_the_worker()

    assert recorder.unexpected == [], f"unpredicted API calls: {recorder.unexpected}"
    # The POST half only. Storing the page-scoped id here would not fail
    # anything until analytics or the first comment tried to use it.
    assert_published(platform_post, FACEBOOK_POST_ID)

    request = recorder.call("/feed")
    assert request.url.path == f"/v25.0/{FACEBOOK_PAGE_ID}/feed", (
        f"posted to {request.url.path}; the connected page's id did not reach the URL"
    )
    assert request.headers["Authorization"] == f"Bearer {FACEBOOK_TOKEN}"

    # Graph names this field `message`. A post body under any other key is
    # accepted as a 200 with an EMPTY post.
    assert json.loads(request.content) == {"message": CAPTION}


# ---------------------------------------------------------------------------
# Instagram - https://developers.facebook.com/docs/instagram-platform/content-publishing
# ---------------------------------------------------------------------------

INSTAGRAM_USER_ID = "17841400000000000"
INSTAGRAM_CONTAINER_ID = "17999888777666555"
INSTAGRAM_MEDIA_ID = "17888777666555444"
INSTAGRAM_TOKEN = "instagram-page-access"


@pytest.mark.django_db(transaction=True)
def test_instagram_creates_a_container_waits_for_it_then_publishes_it(wire):
    """Instagram publishes in THREE steps, and all three have to happen in order.

    POST /{ig-user-id}/media creates a container; the container is polled
    until status_code is FINISHED; POST /{ig-user-id}/media_publish then
    publishes it by `creation_id`. Publishing a container that has not
    finished ingesting returns "media not found" - so the poll is not
    politeness, it is the documented protocol.

    None of that is observable from a status check. The row goes PUBLISHED as
    long as the LAST call returns an id, whether or not the container it named
    was the one just created and whether or not anything waited for it.

    Instagram also refuses a post with no media at all, and the image_url it
    receives is derived by the ENGINE from the stored asset - which is why
    this case attaches a real MediaAsset rather than handing over a URL.
    """
    recorder = wire(
        {
            f"/{INSTAGRAM_USER_ID}/media_publish": lambda request: httpx.Response(
                200,
                json={"id": INSTAGRAM_MEDIA_ID},
            ),
            f"/{INSTAGRAM_USER_ID}/media": lambda request: httpx.Response(
                200,
                json={"id": INSTAGRAM_CONTAINER_ID},
            ),
            # The container reports itself ready immediately. A real one would
            # sit in IN_PROGRESS; the provider's poll loop is 60 attempts two
            # seconds apart, so answering FINISHED at once is what keeps this
            # case at seconds rather than minutes.
            f"/v25.0/{INSTAGRAM_CONTAINER_ID}": lambda request: httpx.Response(
                200,
                json={"status_code": "FINISHED"},
            ),
            # NOT publishing - health check, inbox poll, analytics. See the
            # Mastodon and Facebook cases.
            "/insights": lambda request: httpx.Response(200, json={"data": []}),
            "/conversations": lambda request: httpx.Response(200, json={"data": []}),
            f"/v25.0/{INSTAGRAM_USER_ID}": lambda request: httpx.Response(
                200,
                json={"id": INSTAGRAM_USER_ID, "username": "brightbean", "followers_count": 7},
            ),
            f"/v25.0/{INSTAGRAM_MEDIA_ID}": lambda request: httpx.Response(
                200,
                json={"id": INSTAGRAM_MEDIA_ID},
            ),
        }
    )
    platform_post = schedule_a_post(
        platform="instagram",
        platform_id=INSTAGRAM_USER_ID,
        token=INSTAGRAM_TOKEN,
    )
    attach_an_image(platform_post.post)

    run_the_worker()

    assert recorder.unexpected == [], f"unpredicted API calls: {recorder.unexpected}"
    assert_published(platform_post, INSTAGRAM_MEDIA_ID)

    container = recorder.call(f"/{INSTAGRAM_USER_ID}/media")
    assert container is not None, "no container was created"
    assert container.url.path == f"/v25.0/{INSTAGRAM_USER_ID}/media", (
        f"created the container at {container.url.path}; the connected account's id did not reach the URL"
    )
    assert container.headers["Authorization"] == f"Bearer {INSTAGRAM_TOKEN}"

    body = json.loads(container.content)
    assert body["caption"] == CAPTION
    # The URL is the engine's, not the test's: it reads the asset off storage
    # and makes it absolute. Instagram fetches this itself, so a relative path
    # would be unreachable to it however well-formed the request looked.
    assert body["image_url"].startswith("http"), f"image_url is not absolute: {body['image_url']!r}"
    assert body["image_url"].endswith(".png")

    # The container was polled before it was published. Dropping that wait is
    # a real and documented failure mode, and nothing else here would see it.
    assert recorder.call(f"/v25.0/{INSTAGRAM_CONTAINER_ID}") is not None, (
        "published without ever polling the container's status"
    )

    published = recorder.call("/media_publish")
    assert json.loads(published.content) == {"creation_id": INSTAGRAM_CONTAINER_ID}, (
        "the published creation_id is not the container that was just created"
    )
