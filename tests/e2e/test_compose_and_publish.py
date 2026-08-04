"""End-to-end: a person composes a post in the browser and it reaches the platform.

WRITTEN FROM THE OUTSIDE. This module is not allowed to know the URL names,
the view functions, the form field names or the model layout. It opens the
application at its front door, looks at what is on the screen, and interacts
with what a person could interact with. If a control cannot be found and
clicked from the rendered page, that is a finding about the product, not a
reason to reach into the code for a selector.

That constraint is the whole point. A test that posts the composer's form
fields directly proves the fields it was told about still work; it cannot
notice that the button which submits them is covered, disabled, off-screen or
never rendered.

NOTHING IS INSERTED INTO THE DATABASE. The account, the organization, the
workspace and the connected channel are all created by the product, through
its own screens, because a test that inserts them has not tested creating
them.

EVERY STEP IS PHOTOGRAPHED. Each test writes a numbered trail of full-page
screenshots into its own directory (see the journey fixture), so a run can be
read back afterwards and a failure diagnosed from what was on the screen
rather than from a guess about it.

HOW TO EXTEND IT: drive the browser, screenshot, LOOK at the screenshot, and
pick the control you can see. Do not guess a selector from the template.
"""

import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx
import pytest
from django.core.management import call_command
from django.db import connections
from playwright.sync_api import expect

#: The person this suite plays. A real address is never sent anywhere - the
#: e2e settings keep mail in memory - but it has to look like one.
EMAIL = "e2e-composer@brightbean.test"
PASSWORD = "compose-a-post-8"


def sign_up(page, live_server, journey, email=EMAIL):
    """Create an account through the sign-up form, as a new customer would.

    Labelled controls only. If the email field stops being labelled "Email",
    or the button stops saying "Create Account", this breaks - which is the
    intent: those are the things a person reads.

    THE ADDRESS IS AN ARGUMENT because the serial suite no longer resets the
    database between steps, so every provider's run has to be a DIFFERENT
    person. Reusing one address would meet the product's own "that email is
    taken" and fail on the second provider - correctly, and confusingly.
    """
    page.goto(live_server.url, wait_until="domcontentloaded")
    journey(page, "front-door")

    page.get_by_role("link", name="Sign up").click()
    page.wait_for_load_state("domcontentloaded")
    journey(page, "sign-up-form")

    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    journey(page, "sign-up-filled-in")

    page.get_by_role("button", name="Create Account").click()
    page.wait_for_load_state("networkidle")
    journey(page, "signed-in")


def dismiss_the_onboarding_checklist(page, journey):
    """Close the get-started panel, the way a person closes it.

    IT IS IN THE WAY, in two concrete senses. It floats over the bottom-right
    of every page, covering whatever is beneath it - on the connect screen
    that is a whole platform card. And its first row is a link called
    "Connect social accounts", which shares a name with the ten Connect
    controls on that screen and comes first, so anything reaching for
    "Connect" gets the checklist instead and is taken back to the page it is
    already on.

    Closing it is not a workaround for the test's benefit: it is what a person
    does with an onboarding panel once they know what they came to do.
    """
    close = page.get_by_role("button", name="Dismiss checklist")
    close.highlight()
    journey(page, "the-onboarding-close-control")

    close.click()

    # ASSERT IT ACTUALLY WENT. Pressing a control and assuming it worked is
    # how the earlier version of this test spent three runs clicking the wrong
    # thing. Dismissal is an htmx round trip that swaps the panel out, so this
    # waits for the panel to LEAVE rather than photographing the instant after
    # the click and calling it dismissed.
    expect(close).to_have_count(0)
    journey(page, "onboarding-dismissed")


# ---------------------------------------------------------------------------
# The platforms' own endpoints, answered inside the test process.
#
# This is the ONLY substitution the suite makes, and it is made at the
# platform's boundary rather than anywhere inside the product. Two halves,
# because a connect flow crosses that boundary twice:
#
#   the BROWSER is sent to the platform's authorisation page, which is a
#   navigation Chromium would otherwise attempt over the internet;
#   the SERVER then exchanges the code and calls the platform's API over
#   httpx, in the live_server thread of this same process.
#
# Everything else - the pages, the forms, the database, the worker, the
# engine, each provider's own request building - is real.
# ---------------------------------------------------------------------------

#: What the stand-in authorisation page hands back. The application chooses the
#: state; this echoes it, because rejecting a mismatched state is the product's
#: job and a test must not paper over it.
AUTHORIZATION_CODE = "e2e-authorization-code"


def is_the_application(url):
    """True when a URL belongs to the application under test, not a platform."""
    host = urlsplit(url).hostname or ""
    return host in {"127.0.0.1", "localhost", "testserver"}


class PlatformEndpoints:
    """Answers what the platform would answer, and records everything.

    BOTH SIDES ARE RECORDED, and that is not incidental. An early version
    recorded only the server's calls, so when the connect flow did nothing at
    all the evidence was an empty list - which is indistinguishable from a
    flow that ran and made no calls. Recording the browser's navigations too
    is what tells those two apart.

    `routes` maps a URL-path suffix to a handler. Anything unrouted is
    answered 404 and recorded, so an endpoint this test did not predict
    surfaces instead of being quietly satisfied.
    """

    def __init__(self, routes):
        #: path-suffix -> (which platform's table it came from, responder).
        #: WHOSE route answered is recorded, because a suffix belongs to
        #: whoever claimed it first and "/me" is claimed by three platforms.
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.sent: list[bytes] = []
        self.navigations: list[str] = []
        self.unexpected: list[str] = []
        self.answered_by: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        # READ THE BODY NOW, while it is still there. Asking a request for its
        # content AFTER the transport has dealt with it can answer empty, and
        # that is not hypothetical: LinkedIn's image upload PUTs the file's
        # bytes, the recorder held the request object, and a later check for a
        # PNG signature found nothing - reporting "the platform was never
        # given the picture" about the one provider that had uploaded the
        # whole file.
        try:
            self.sent.append(request.content)
        except Exception:
            self.sent.append(b"")
        for suffix, (owner, responder) in self.routes.items():
            if request.url.path.endswith(suffix):
                self.answered_by.append(owner)
                return responder(request)
        self.unexpected.append(f"{request.method} {request.url}")
        return httpx.Response(404, json={"error": "unrouted in this test"})

    def call(self, suffix):
        for request in self.requests:
            if request.url.path.endswith(suffix):
                return request
        return None

    def exchanged_a_token(self):
        """True when the application traded the code for a token.

        Recognised by the SHAPE of the request rather than by one platform's
        path, because every OAuth platform spells its token endpoint
        differently - /oauth/access_token, /v2/oauth/token/, /token - and this
        has to hold for any provider on the books, not just the one in front
        of it. What they share is a POST carrying the authorization code.
        """
        return any(
            request.method == "POST" and AUTHORIZATION_CODE in (request.url.query.decode() + self.body_of(request))
            for request in self.requests
        )

    @staticmethod
    def body_of(request):
        try:
            return request.content.decode(errors="replace")
        except Exception:
            return ""

    def what_happened(self):
        """Everything the platform boundary saw, for a failure message."""
        return (
            f"\n  browser was sent to: {self.navigations or 'nothing'}"
            f"\n  server called: {[str(r.url) for r in self.requests] or 'nothing'}"
        )


@pytest.fixture(scope="class")
def platforms(a_page):
    """Install both halves of the platform boundary and return the recorder.

    CLASS SCOPE, because the suite below is one continuous session per
    provider: the browser, the account and the connected channel all outlive a
    single test, so the boundary they talk through has to as well. monkeypatch
    is function-scoped and cannot be used here, so the same undoing is done by
    hand.
    """
    page = a_page
    undo = pytest.MonkeyPatch()

    def install(routes):
        endpoints = PlatformEndpoints(routes)

        def authorisation_page(route):
            """Stand in for the platform's consent screen.

            A real one asks the person to approve and then sends the browser
            back to the redirect_uri with a code. Both values are taken FROM
            THE REQUEST the application made, so this cannot accidentally
            supply a redirect_uri or a state the product did not choose.
            """
            endpoints.navigations.append(route.request.url)
            query = parse_qs(urlsplit(route.request.url).query)
            redirect_uri = query.get("redirect_uri", [""])[0]
            state = query.get("state", [""])[0]
            route.fulfill(
                status=302,
                headers={"Location": f"{redirect_uri}?code={AUTHORIZATION_CODE}&state={state}"},
            )

        def route_by_destination(route):
            """Only an AUTHORISATION request is a platform boundary.

            An earlier version treated every outbound request as one, and so
            answered the page's own flatpickr and chart.js with an OAuth
            redirect. That corrupts the page's scripts, including the date
            picker any scheduling test needs, while looking like a working
            test. Both are served by this application now, so they never reach
            this handler at all - the hazard was real while they came from a
            public CDN, and the rule it taught is kept.

            An authorisation request is recognisable without knowing anything
            about this product: it is the one carrying a redirect_uri.

            NOTHING ON A PAGE COMES FROM A THIRD PARTY any more - flatpickr,
            chart.js and Swagger UI are vendored, see
            static/js/vendor/README.md - so a browser run no longer depends on
            the public internet being up. The only outbound traffic left is
            what the product sends to the platforms, which is the whole point
            of this recorder.
            """
            url = route.request.url

            # THE REDIRECT IS CAUGHT WHERE IT IS ISSUED, not where it lands.
            #
            # Pressing Connect POSTs to our own server, which answers 302 to
            # the platform's consent page. Chromium follows that hop
            # internally and NEITHER page.route NOR context.route fires for
            # it - both were measured, and both left the browser parked on
            # facebook.com having never consulted this handler.
            #
            # route.fetch(max_redirects=0) performs the POST here instead, so
            # the 302 arrives in this process and the browser has not moved
            # yet. If it points at a platform's authorisation page, this
            # answers the browser with what the platform would have answered
            # once the person approved: a redirect back to the redirect_uri
            # carrying a code.
            if is_the_application(url) and route.request.method == "POST":
                response = route.fetch(max_redirects=0)
                going_to = response.headers.get("location", "")
                if not is_the_application(going_to) and "redirect_uri=" in going_to:
                    endpoints.navigations.append(going_to)
                    query = parse_qs(urlsplit(going_to).query)
                    route.fulfill(
                        status=302,
                        headers={
                            "Location": (
                                f"{query.get('redirect_uri', [''])[0]}"
                                f"?code={AUTHORIZATION_CODE}"
                                f"&state={query.get('state', [''])[0]}"
                            )
                        },
                    )
                    return
                route.fulfill(response=response)
                return

            if not is_the_application(url) and "redirect_uri=" in url:
                authorisation_page(route)
            else:
                route.continue_()

        # At CONTEXT level, not page level. Measuring whether that is what
        # makes a redirected navigation interceptable: our server answers the
        # connect form with a 302 to the platform, and a page-level route
        # never fired for the hop that followed.
        page.context.route(re.compile(r".*"), route_by_destination)

        # providers/base.py builds httpx.Client inline per request, so there is
        # no transport to inject: the class itself is replaced, process-wide,
        # which is what reaches both the live_server thread and the worker's.
        real_client = httpx.Client

        def client_with_mock_transport(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(endpoints.handle)
            return real_client(*args, **kwargs)

        undo.setattr(httpx, "Client", client_with_mock_transport)
        return endpoints

    yield install
    undo.undo()


#: The name the platform gives back for the account being installed. The
#: product should show it once the installation is done - that is how a person
#: knows it worked.
ACCOUNT_ON_THE_PLATFORM = "Brightbean Test Page"

#: What the person writes. Distinctive on purpose: it has to be recognisable
#: on the preview pane, in the queue, and in the body of the request that
#: reaches the platform, so that finding it there proves it travelled the
#: whole way rather than merely being typed.
A_POST_ABOUT_COFFEE = "Fresh beans, brewed bright. Come and taste the new roast."

#: As much of it as a calendar chip has room for. The chip truncates, so this
#: is what can honestly be looked for there.
THE_OPENING_WORDS = "Fresh beans"

#: A SECOND post, written to be published immediately rather than queued.
#: Distinct from the first so that finding it in a request to the platform
#: cannot be satisfied by the queued one.
A_POST_TO_PUBLISH_NOW = "Doors open at seven. The espresso machine is already warm."

#: A post that is kept rather than sent. Distinct from the others so that
#: finding it under Drafts cannot be satisfied by one of them.
A_POST_LEFT_AS_A_DRAFT = "Thinking about a Saturday cupping session - not decided yet."

#: WHAT GOES IN THE FIRST COMMENT, which is a separate request to the platform
#: made by a separate worker some time after the post itself. Distinct wording
#: on purpose: finding these words in a request proves the COMMENT arrived,
#: and cannot be satisfied by the post that carried the caption.
A_FIRST_COMMENT = "Beans from the Kiunyu washing station, if anyone asks."

#: A post with a picture, for the platforms that will not take words alone.
A_POST_WITH_A_PICTURE = "Latte art, first attempt of the morning."

#: DEV.to posts are ARTICLES, and an article has a title. The composer grows a
#: field for it as soon as a DEV.to channel is ticked.
A_TITLE_FOR_AN_ARTICLE = "Notes from the roastery"

#: Written to be queued with the title left empty, which DEV.to will not take.
AN_ARTICLE_NOBODY_TITLED = "A thought I have not titled yet."

#: The picture itself. Written to a temp file rather than committed, because a
#: binary in the repository is a thing nobody can read in a diff. It is a real
#: PNG - the composer, the media library and the platform all get to decide
#: whether they like it, which is the point of handing them a real file.
AN_IMAGE = Path(tempfile.gettempdir()) / "brightbean-e2e-latte.png"


def an_image_on_disk():
    """A file for the OS file-picker to hand over, as a person would.

    DRAWN, not pasted in as base64. The first version of this carried a
    hand-assembled base64 blob, and the composer showed it as a broken image
    in both the attachment strip and the preview - which looks exactly like a
    product defect in thumbnailing and was nothing of the sort. Pillow is
    already a dependency of this project, so a genuinely valid PNG costs one
    line and removes the doubt entirely.

    600x600 because the media-first platforms have minimum dimensions, and a
    1x1 pixel would invite a refusal that says nothing about the feature.
    """
    # WRITTEN EVERY TIME, not "if it is missing". Guarding on existence meant
    # the first run's file survived in the temp directory and every later run
    # uploaded it - so replacing the broken base64 with a real drawing changed
    # nothing on screen, and the picture stayed broken for a reason that no
    # longer existed in the source.
    from PIL import Image

    Image.new("RGB", (600, 600), (122, 79, 46)).save(AN_IMAGE)
    return str(AN_IMAGE)


#: The video, for the two platforms that take nothing else. Encoded on every
#: run for the same reason the picture is drawn on every run.
A_VIDEO = Path(tempfile.gettempdir()) / "brightbean-e2e-tone.mp4"

#: HOW LONG A PERSON WOULD WAIT FOR AN ATTACHMENT TO APPEAR, in milliseconds.
#:
#: Generous on purpose. Storing a video is not storing a picture: the
#: application runs ffmpeg over it for the thumbnail and the duration, and its
#: own timeout for that is MEDIA_LIBRARY_FFMPEG_TIMEOUT - five minutes. So a
#: second or two is not a bound anybody chose, it is a bound nobody noticed.
#:
#: This is a ceiling, not a delay: the wait ends the moment the thumbnail is
#: on the screen. It exists so a slow upload FAILS LOUDLY here instead of
#: letting the next step publish a post the file never reached.
UPLOAD_PATIENCE = 60_000


def a_video_on_disk():
    """A real MP4 - a flat colour and a sine tone - to hand to the picker."""
    from tests.e2e.make_test_video import make_test_video

    return make_test_video(A_VIDEO)


def what_this_platform_takes(spec):
    """The file a person would attach here, and how the browser should name it.

    Returns (path, mime type). TikTok and YouTube take video and refuse a
    picture as flatly as they refuse a text post; everybody else takes the
    picture.
    """
    if spec["platform"] in WANTS_VIDEO:
        return a_video_on_disk(), "video/mp4"
    return an_image_on_disk(), "image/png"


def finish_installing(page, journey):
    """Complete the installation on whatever the platform's return lands on.

    Some platforms hand back several accounts and the product asks which to
    install; others come straight back connected. Both are real, so this
    handles the choice WHEN IT IS OFFERED rather than assuming either.
    """
    choose = page.get_by_role("button", name="Connect Selected")
    if choose.count() == 0:
        journey(page, "returned-from-the-platform")
        return

    journey(page, "asked-which-account-to-install")
    # Tick the account before confirming. Pressing confirm with nothing
    # selected is a different test, and one worth writing.
    page.get_by_role("checkbox").first.check()
    journey(page, "account-chosen")

    choose.click()
    page.wait_for_load_state("networkidle")


def a_resumable_upload(session_uri, created):
    """Answer Google's two-step resumable upload, which shares ONE path.

    Source: developers.google.com, YouTube Data API > Resumable Uploads.
    Step 1 is a POST carrying the video resource and the X-Upload-Content-*
    headers; it answers 200 with an empty body and the session URI in the
    LOCATION HEADER. Step 3 PUTs the file's bytes to that URI and answers 201
    with the created video resource.

    Both requests have the same path, so the two are told apart by method -
    which is also why this cannot be written as a plain answers() row.
    """

    def responder(request):
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_uri, "Content-Length": "0"}, content=b"")
        return httpx.Response(201, json=created)

    return responder


def headers_of(request):
    """Every header of a request as one string, with the values intact.

    Built from the items rather than from the object's own representation,
    because what this is used for is looking for a credential somebody typed
    in - and a representation that abbreviates or hides a header value would
    answer "never sent" about a request that carried it.
    """
    return " ".join(f"{name}: {value}" for name, value in request.headers.items())


def answers(payload, status=200):
    """A platform endpoint that always answers the same document."""
    return lambda request: httpx.Response(status, json=payload)


def whoever_the_token_belongs_to(request):
    """Graph API /me answers as the OWNER OF THE TOKEN, and that matters here.

    A USER token makes /me the person; a PAGE token makes it the Page. That is
    not a detail - it is the whole reason a Page channel keeps its name.

    THIS SUITE ANSWERED /me WITH THE PERSON REGARDLESS, and the moment the
    background worker started running, that broke the run: the account health
    check calls get_profile() and writes the result back over the channel's
    name, so the connected Page "Brightbean Test Page" silently became
    "Brightbean Tester" between one step and the next, and every later step
    looked for a channel that no longer existed.

    The product is right and the stub was wrong. Nothing in the application
    changed for this.
    """
    carrying = headers_of(request) + str(request.url)
    if "e2e-page-token" in carrying:
        return httpx.Response(
            200,
            json={
                "id": "111222333444555",
                "name": ACCOUNT_ON_THE_PLATFORM,
                "followers_count": 7,
                "picture": {"data": {"url": ""}},
            },
        )
    return httpx.Response(200, json={"id": "e2e-user", "name": "Brightbean Tester", "picture": {"data": {"url": ""}}})


#: A token response, which every OAuth platform answers in the same shape.
A_TOKEN = answers({"access_token": "e2e-user-token", "token_type": "bearer", "expires_in": 5184000})

#: WHAT DIFFERS BETWEEN PLATFORMS, and nothing else does.
#:
#: The journey is identical for all of them - press the card's button, get
#: sent to the platform to authorise, come back with a code, have it
#: exchanged. Only two things vary: the name on the card, and the endpoints
#: that platform serves. So those are a table and the journey is one test.
#:
#: A platform missing from here is NOT covered. Adding one is adding a row.
#:
#: `text_only` says whether the platform accepts a post that is nothing but
#: words. Several do not, and the product is right to refuse them: Instagram
#: and Pinterest need an image, TikTok and YouTube need a video. Driving a
#: text post at those and calling the refusal a failure would be testing the
#: test. Their media journeys are the next feature steps to write, and until
#: those exist this flag is what says so out loud rather than silently.
PLATFORMS = {
    "instagram": {
        "card": "Instagram",
        "endpoints": {
            "/oauth/access_token": A_TOKEN,
            # Instagram publishes in two moves: a container carrying the
            # image, then a publish of that container. The longer suffix comes
            # first because a suffix match takes the first route that fits, and
            # the container is polled for status until it says FINISHED.
            "/media_publish": answers({"id": "17900000000000001"}),
            "/media": answers({"id": "17800000000000001", "status_code": "FINISHED"}),
            # And the container is then POLLED BY ITS OWN ID until it reports
            # FINISHED - Instagram ingests media asynchronously, so publishing
            # before that returns "media not found". The id below is the one
            # answered above, which is why this route can be written at all.
            "/17800000000000001": answers({"status_code": "FINISHED", "status": "Finished"}),
            "/me/accounts": answers(
                {
                    "data": [
                        {
                            "id": "111222333444555",
                            "name": "A Facebook Page",
                            "access_token": "e2e-page-token",
                            "category": "Software",
                            "picture": {"data": {"url": ""}},
                            "instagram_business_account": {
                                "id": "17841400000000000",
                                "username": "brightbean_test",
                                "name": ACCOUNT_ON_THE_PLATFORM,
                                "profile_picture_url": "",
                                "followers_count": 7,
                                "media_count": 2,
                            },
                        }
                    ]
                }
            ),
        },
    },
    "instagram_login": {
        "card": "Instagram (Direct)",
        "endpoints": {
            "/oauth/access_token": A_TOKEN,
            # Instagram Login trades the short-lived token for a long-lived one
            # at a DIFFERENT path on a different host. Listed after the one
            # above because a suffix match takes the first route that fits and
            # "/access_token" would otherwise swallow "/oauth/access_token".
            "/access_token": A_TOKEN,
            "/me": answers(
                {
                    "user_id": "17841400000000000",
                    "username": "brightbean_test",
                    "name": ACCOUNT_ON_THE_PLATFORM,
                    "profile_picture_url": "",
                    "followers_count": 7,
                    "media_count": 2,
                    "biography": "",
                }
            ),
        },
    },
    "linkedin_personal": {
        "card": "LinkedIn (Personal Profile)",
        "endpoints": {
            "/oauth/v2/accessToken": A_TOKEN,
            # LinkedIn answers a created post with the URN in a header rather
            # than the body, which is why this one carries headers at all.
            "/rest/posts": lambda request: httpx.Response(
                201, json={}, headers={"x-restli-id": "urn:li:share:7000000000000000000"}
            ),
            # A PICTURE ON LINKEDIN IS THREE MOVES: ask where to put it, PUT
            # the bytes there, then create a post referring to it by URN.
            "/rest/images": answers(
                {"value": {"uploadUrl": "https://api.linkedin.com/mediaUpload/e2e", "image": "urn:li:image:e2e"}}
            ),
            "/mediaUpload/e2e": answers({}),
            # OIDC mode, which is what a dev app without Community Management
            # approval gets: the profile comes from the userinfo claims.
            "/v2/userinfo": answers({"sub": "e2e-member", "name": ACCOUNT_ON_THE_PLATFORM, "picture": ""}),
        },
    },
    "linkedin_company": {
        "card": "LinkedIn (Company Page)",
        "endpoints": {
            "/oauth/v2/accessToken": A_TOKEN,
            "/v2/organizationalEntityAcls": answers(
                {
                    "elements": [
                        {
                            "organizationalTarget": "urn:li:organization:99887766",
                            "organizationalTarget~": {
                                "id": 99887766,
                                "localizedName": ACCOUNT_ON_THE_PLATFORM,
                                "vanityName": "brightbean-test",
                            },
                        }
                    ]
                }
            ),
        },
    },
    "tiktok": {
        "card": "TikTok",
        "endpoints": {
            "/oauth/token/": A_TOKEN,
            # WHAT TIKTOK LETS THIS CREATOR DO, asked fresh every time a post
            # is composed - TikTok's own integration rules require it, because
            # the allowed privacy levels depend on the app's audit status and
            # the account's settings. Unanswered, the composer's required
            # "Who can see this post" field sits on "Loading account
            # settings..." for ever, and nothing can be saved at all - not
            # even a draft.
            "/creator_info/query/": answers(
                {
                    "data": {
                        "creator_nickname": ACCOUNT_ON_THE_PLATFORM,
                        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
                        "comment_disabled": False,
                        "duet_disabled": False,
                        "stitch_disabled": False,
                        "max_video_post_duration_sec": 600,
                    }
                }
            ),
            "/user/info/": answers(
                {
                    "data": {
                        "user": {
                            "open_id": "e2e-open-id",
                            "union_id": "e2e-union-id",
                            "avatar_url": "",
                            "display_name": ACCOUNT_ON_THE_PLATFORM,
                        }
                    }
                }
            ),
            # PUBLISHING A VIDEO, and this pair is the first thing in this
            # file taken from a PLATFORM'S OWN REFERENCE rather than from
            # watching our code and answering whatever it asked for. Source:
            # developers.tiktok.com, Content Posting API > API Reference >
            # Video > Direct Post, sections "Response" and "Send Video to
            # TikTok Servers"; photographed into .api-reference/ by
            # tests/e2e/read_the_docs.py so it can be checked rather than
            # taken on trust.
            #
            # The documented envelope is data{publish_id, upload_url} beside
            # error{code, message, log_id}, where any code but "ok" is a
            # failure and upload_url is present ONLY for source=FILE_UPLOAD.
            # publish_id follows the documented "v_pub_file~v2-1.<digits>"
            # shape, which our provider's analytics path parses.
            #
            # DOCUMENTED INCONSISTENCY, left as TikTok has it: the field table
            # names the third error field "logid", the worked example calls it
            # "log_id". The example is followed here.
            "/post/publish/video/init/": answers(
                {
                    "data": {
                        "publish_id": "v_pub_file~v2-1.123456789",
                        # A SECOND DOCUMENTED INCONSISTENCY: this section's own
                        # example returns ".../video/?upload_id=...", while the
                        # upload example a page later PUTs to
                        # ".../upload/?upload_id=...". The upload example wins
                        # here, being the one that describes the request.
                        "upload_url": "https://open-upload.tiktokapis.com/upload/?upload_id=e2e&upload_token=e2e",
                    },
                    "error": {"code": "ok", "message": "", "log_id": "e2e-tiktok-log"},
                }
            ),
            # And the video itself is PUT to the whole of that URL, query
            # string included - the reference says so twice, in the table
            # ("HTTP URL: Returned in upload_url", "HTTP Method: PUT") and in
            # a note warning not to drop the query parameters.
            "/upload/": answers({}),
        },
    },
    "youtube": {
        "card": "YouTube",
        "endpoints": {
            "/token": A_TOKEN,
            # UPLOADING A VIDEO, from Google's own reference rather than from
            # watching our code: developers.google.com, YouTube Data API >
            # Resumable Uploads, steps 1 to 4. Photographed and extracted into
            # .api-reference/ by tests/e2e/read_the_docs.py.
            #
            # The session URI Google's example returns is the same path again
            # with an upload_id, so step 1 and step 3 are indistinguishable by
            # path and are answered by method - see a_resumable_upload.
            "/upload/youtube/v3/videos": a_resumable_upload(
                "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=e2e",
                {
                    "kind": "youtube#video",
                    "id": "e2e-video-id",
                    "snippet": {
                        "title": A_POST_WITH_A_PICTURE,
                        "description": A_POST_WITH_A_PICTURE,
                        "categoryId": "22",
                    },
                    "status": {"privacyStatus": "public", "embeddable": True, "license": "youtube"},
                },
            ),
            "/channels": answers(
                {
                    "items": [
                        {
                            "id": "UCe2e0000000000000000000",
                            "snippet": {
                                "title": ACCOUNT_ON_THE_PLATFORM,
                                "customUrl": "@brightbeantest",
                                "thumbnails": {"default": {"url": ""}},
                            },
                            "statistics": {"subscriberCount": "7", "viewCount": "42", "videoCount": "2"},
                        }
                    ]
                }
            ),
        },
    },
    "pinterest": {
        "card": "Pinterest",
        "endpoints": {
            "/oauth/token": A_TOKEN,
            # THE COMPOSER ASKS FOR BOARDS as soon as a Pinterest channel has
            # a picture on it - a pin has to go somewhere. Unanswered, the
            # provider raised and our own server handed the browser a 502,
            # which is how this surfaced: as a refusal on the page, not as a
            # platform call.
            "/boards": answers({"items": [{"id": "e2e-board-1", "name": "Coffee"}]}),
            "/pins": answers({"id": "e2e-pin-1"}),
            "/user_account": answers(
                {
                    "id": "e2e-pinner",
                    "username": "brightbean_test",
                    "business_name": ACCOUNT_ON_THE_PLATFORM,
                    "profile_image": "",
                    "follower_count": 7,
                }
            ),
        },
    },
    "threads": {
        "card": "Threads",
        "endpoints": {
            "/oauth/access_token": A_TOKEN,
            "/access_token": A_TOKEN,
            # Threads publishes in two moves: a container, then a publish of
            # that container. The longer suffix is listed first because a
            # suffix match takes the first route that fits.
            "/threads_publish": answers({"id": "e2e-thread-1"}),
            "/threads": answers({"id": "e2e-container-1", "status": "FINISHED"}),
            # A media container is polled by its own id until it says FINISHED,
            # exactly as Instagram's is - Threads ingests pictures and video
            # asynchronously and refuses to publish one that is not ready.
            "/e2e-container-1": answers({"status": "FINISHED", "error_message": ""}),
            "/me": answers(
                {
                    "id": "e2e-threads-user",
                    "username": "brightbean_test",
                    "name": ACCOUNT_ON_THE_PLATFORM,
                    "threads_profile_picture_url": "",
                    "threads_biography": "",
                }
            ),
        },
    },
    "google_business": {
        "card": "Google Business Profile",
        "endpoints": {
            "/token": A_TOKEN,
            "/accounts": answers({"accounts": [{"name": "accounts/99887766"}]}),
            "/localPosts": answers({"name": "accounts/99887766/locations/12345/localPosts/1", "searchUrl": ""}),
            "/locations": answers(
                {
                    "locations": [
                        {
                            "name": "locations/12345",
                            "title": ACCOUNT_ON_THE_PLATFORM,
                            "storefrontAddress": {"addressLines": ["1 Test Street"]},
                            "phoneNumbers": {"primaryPhone": "+49 30 000000"},
                        }
                    ]
                }
            ),
        },
    },
    "facebook": {
        "card": "Facebook",
        "endpoints": {
            "/oauth/access_token": A_TOKEN,
            "/me/accounts": answers(
                {
                    "data": [
                        {
                            "id": "111222333444555",
                            "name": "Brightbean Test Page",
                            "access_token": "e2e-page-token",
                            "category": "Software",
                            "followers_count": 7,
                            "picture": {"data": {"url": ""}},
                        }
                    ]
                }
            ),
            # WHERE A PUBLISHED POST ACTUALLY GOES. Without this the engine's
            # publish attempt met a 404 from the recorder, the post failed and
            # stayed in the queue, and the Sent tab said "No sent posts yet" -
            # which looked exactly like a product defect and was reported as
            # one, twice, before the recorder's own unpredicted list was read.
            "/feed": answers({"id": "111222333444555_98765432109876543"}),
            "/photos": answers({"id": "98765432109876543", "post_id": "111222333444555_98765432109876543"}),
            "/videos": answers({"id": "98765432109876543"}),
            # EVERYTHING BELOW HERE IS REACHED BY THE BACKGROUND WORKER, and
            # none of it was reachable at all until this suite started running
            # process_tasks. The engine hands the first comment to that worker,
            # and draining its queue also releases the recurring inbox sync and
            # analytics collection - so these are not extra endpoints somebody
            # invented, they are what the product does when both its workers
            # are running.
            #
            # THE FIRST COMMENT. Graph API: POST /{object-id}/comments answers
            # the new comment's id. This is the one the publisher's own task
            # calls, two minutes after the post in production and immediately
            # here (see PUBLISHER_FIRST_COMMENT_DELAY in the e2e settings).
            "/comments": answers({"id": "111222333444555_98765432109876544"}),
            # THE INBOX. apps/inbox/tasks.py syncs conversations on a repeat
            # schedule. An EMPTY list is a real answer - a page with no
            # messages - and it covers that the call is made and parsed. It
            # does NOT cover reading a message, which needs its own step and
            # is not written yet.
            "/conversations": answers({"data": []}),
            # ANALYTICS. providers/meta_insights.py asks for one metric at a
            # time, and providers/facebook.py asks for per-post insights.
            # Empty data again: enough to prove the collection runs and
            # handles what comes back, not enough to claim the numbers are
            # right. THESE SHAPES ARE NOT DOCUMENTED-GROUNDED like TikTok's
            # and YouTube's are - they are the Graph API's general envelope,
            # written from knowledge rather than photographed, and that is
            # said out loud rather than implied.
            "/insights": answers({"data": []}),
            # And the objects those collectors read back by id: the page for
            # its follower count, the post for its own fields. Listed after
            # the two above so a path ending in /insights is not swallowed.
            "/111222333444555_98765432109876543": answers(
                {
                    "id": "111222333444555_98765432109876543",
                    "message": A_POST_TO_PUBLISH_NOW,
                    "created_time": "2026-08-04T07:00:00+0000",
                    "permalink_url": "",
                    "shares": {"count": 0},
                    "comments": {"summary": {"total_count": 0}},
                    "reactions": {"summary": {"total_count": 0}},
                }
            ),
            "/98765432109876543": answers(
                {
                    "id": "98765432109876543",
                    "message": A_POST_WITH_A_PICTURE,
                    "created_time": "2026-08-04T07:00:00+0000",
                    "permalink_url": "",
                    "shares": {"count": 0},
                    "comments": {"summary": {"total_count": 0}},
                    "reactions": {"summary": {"total_count": 0}},
                }
            ),
            "/111222333444555": answers({"id": "111222333444555", "followers_count": 7}),
            # ANSWERED BY THE TOKEN, not by a fixed document - see
            # whoever_the_token_belongs_to for what that cost when it wasn't.
            "/me": whoever_the_token_belongs_to,
        },
    },
    # THE THREE THAT ARE NOT OAUTH AT ALL, and that nothing has ever
    # installed. A person is not sent to the platform to approve anything:
    # they type something into a form on our own screen - Mastodon needs the
    # instance to join, Bluesky an app password, DEV.to an API key.
    #
    # `connect` says how the channel is installed, and it is the only thing
    # that differs. What each form ASKS FOR is discovered by driving it and
    # reading the screen; the endpoints below are discovered by leaving them
    # empty and letting the recorder name what went unanswered.
    # Mastodon is FEDERATED, so there is no one place to be sent to: the
    # instance has to be named first, and only then does the browser leave for
    # it. Its screen says so - "After entering your instance URL, you'll be
    # redirected to your Mastodon instance to authorize access".
    "mastodon": {
        "card": "Mastodon",
        # THE INSTANCE ONLY. The field's own placeholder shows
        # "mastodon.social@yourusername", and typing that SHAPE is refused:
        # read as a URL, everything after the @ is the host, so it names a
        # host of "yourusername" and the application answers "Invalid instance
        # URL. Private or reserved addresses are not allowed." The placeholder
        # demonstrates a value the form will not take.
        "typed_in": {"Profile URL": "mastodon.social"},
        "submitted_with": "Continue to Mastodon",
        "endpoints": {
            # THE INSTANCE HAS NEVER HEARD OF US, so before anybody can be
            # sent there to approve anything, the application registers itself
            # as an app ON that instance and gets a client id back. That is
            # the step the other nine platforms do not have.
            "/api/v1/apps": answers(
                {
                    "id": "1",
                    "name": "Brightbean",
                    "client_id": "e2e-mastodon-client",
                    "client_secret": "e2e-mastodon-secret",
                    "redirect_uri": "",
                    "vapid_key": "",
                }
            ),
            "/oauth/token": A_TOKEN,
            # A post on Mastodon is a STATUS, and a picture is uploaded to the
            # instance first and then referred to by id.
            "/api/v1/statuses": answers(
                {"id": "1", "url": "https://mastodon.social/@brightbean_test/1", "content": ""}
            ),
            "/api/v2/media": answers({"id": "1", "type": "image", "url": "", "preview_url": ""}),
            "/api/v1/accounts/verify_credentials": answers(
                {
                    "id": "1",
                    "username": "brightbean_test",
                    "acct": "brightbean_test",
                    "display_name": ACCOUNT_ON_THE_PLATFORM,
                    "avatar": "",
                    "followers_count": 7,
                }
            ),
        },
    },
    # Bluesky and DEV.to never leave our site at all. A person pastes a
    # credential they made on the platform, and the application has to go and
    # check it - which is what test_03 asserts for them instead of an
    # authorisation redirect that never comes.
    "bluesky": {
        "card": "Bluesky",
        "typed_in": {
            "Handle": "brightbean-test.bsky.social",
            "App Password": "e2e1-e2e2-e2e3-e2e4",
        },
        "submitted_with": "Connect Bluesky",
        "sends_you_to_the_platform": False,
        "endpoints": {
            # THE APP PASSWORD IS TRADED FOR A SESSION, which is how the
            # platform says whether it is any good - and the reason a person
            # can be told straight away that it is not.
            "/xrpc/com.atproto.server.createSession": answers(
                {
                    "accessJwt": "e2e-access-jwt",
                    "refreshJwt": "e2e-refresh-jwt",
                    "handle": "brightbean-test.bsky.social",
                    "did": "did:plc:e2ee2ee2ee2ee2ee2ee2",
                }
            ),
            # A POST IS A RECORD IN THE PERSON'S OWN REPOSITORY, which is what
            # "AT Protocol" means on the card: there is no /posts endpoint,
            # the post is written into their repo and the picture is a blob
            # uploaded to it first.
            "/xrpc/com.atproto.repo.createRecord": answers(
                {
                    "uri": "at://did:plc:e2ee2ee2ee2ee2ee2ee2/app.bsky.feed.post/e2e",
                    "cid": "e2e-cid",
                }
            ),
            "/xrpc/com.atproto.repo.uploadBlob": answers(
                {
                    "blob": {
                        "$type": "blob",
                        "ref": {"$link": "e2e-blob-link"},
                        "mimeType": "image/png",
                        "size": 2338,
                    }
                }
            ),
            # And the session is then asked who it belongs to - the handle a
            # person typed is not taken as proof of anything.
            "/xrpc/com.atproto.server.getSession": answers(
                {
                    "handle": "brightbean-test.bsky.social",
                    "did": "did:plc:e2ee2ee2ee2ee2ee2ee2",
                    "email": "e2e-bluesky@brightbean.test",
                }
            ),
            "/xrpc/app.bsky.actor.getProfile": answers(
                {
                    "did": "did:plc:e2ee2ee2ee2ee2ee2ee2",
                    "handle": "brightbean-test.bsky.social",
                    "displayName": ACCOUNT_ON_THE_PLATFORM,
                    "avatar": "",
                    "followersCount": 7,
                }
            ),
        },
    },
    "devto": {
        "card": "DEV.to",
        "typed_in": {"API Key": "e2e-devto-api-key"},
        "submitted_with": "Connect DEV.to",
        "sends_you_to_the_platform": False,
        "endpoints": {
            # THE WHOLE SUFFIX, not "/me". DEV.to installed happily against
            # Instagram's "/me" stub while this table was empty, because a
            # route belongs to whoever claims the tail of the path first -
            # which is exactly what answered_by now refuses to let pass.
            "/api/users/me": answers(
                {
                    "id": 1,
                    "username": "brightbean_test",
                    "name": ACCOUNT_ON_THE_PLATFORM,
                    "profile_image": "",
                }
            ),
            # A post here is an ARTICLE - a title and a Markdown body, which
            # is what the connect screen says it will be.
            "/api/articles": answers(
                {
                    "id": 1,
                    "title": A_TITLE_FOR_AN_ARTICLE,
                    "url": "https://dev.to/brightbean_test/e2e",
                }
            ),
        },
    },
}


#: The platforms THIS PRODUCT will not send a post of words alone to, read off
#: their own composer panels: Instagram and Pinterest want an image, TikTok and
#: YouTube a video. TikTok's panel, for instance, grows a required "Who can see
#: this post" field and a COVER chooser the moment the channel is picked.
#:
#: That is a statement about the product, NOT about the platforms. YouTube has
#: community posts - text, images, polls - and this product does not offer
#: them; its YouTube provider handles video and shorts only. Instagram and
#: Pinterest genuinely are media-first, but YouTube's absence here is a gap in
#: what is built, and naming it in the harness is the honest place to record
#: it until somebody decides whether to close it.
NEEDS_MEDIA = {"instagram", "instagram_login", "pinterest", "tiktok", "youtube"}

#: And of those, the ones that take VIDEO and nothing else. A picture is
#: refused by these two exactly as a text post was, so the suite makes them a
#: video instead - see tests/e2e/make_test_video.py, which encodes one rather
#: than committing a binary nobody can read in a diff.
#:
#: Pinterest used to be listed here on the assumption that a pin needs more
#: than a picture. It does not: the board the composer already asks for is
#: enough, and taking Pinterest out of this set made its publish step pass
#: with no other change. An assumption in a skip list is a feature nobody is
#: testing.
WANTS_VIDEO = {"tiktok", "youtube"}

#: WHERE THE SLOW, DETERMINISTIC ATTACH IS PROVEN - once, not thirteen times.
#:
#: test_06 can attach a file either before the composer's first autosave or
#: after it, and those are different paths through the product: before, the
#: upload parks in the session with no post to belong to; after, the post
#: exists and the page has been told its id. Reaching the second one means
#: waiting for autosave's THIRTY-SECOND tick.
#:
#: That wait is worth thirty seconds. It is not worth thirteen times thirty,
#: which is six and a half minutes added to a seven-minute suite and an e2e
#: job that would then sit on its own twenty-minute timeout. The composer is
#: the same code for every platform - what varies per platform is its own
#: panel and its provider, neither of which is involved here - so this is a
#: property of the product, proven on one platform, exactly as test_09 proves
#: the article-title refusal only where articles exist.
#:
#: YouTube because that is where it was first suspected, and because a video
#: is the attachment with the most to lose.
THE_PLATFORM_THAT_WAITS_FOR_THE_DRAFT = "youtube"


@pytest.fixture(scope="class", params=sorted(PLATFORMS))
def spec(request):
    """The provider this run of the suite is about - one run per row."""
    return {"platform": request.param, **PLATFORMS[request.param]}


@pytest.fixture(scope="class")
def endpoints(platforms, spec):
    """Answer EVERY platform's endpoints, with this provider's taking precedence.

    The worker does not publish one provider's posts. It polls for everything
    that is due, across the whole database - and this suite no longer resets
    the database between providers, so by the time LinkedIn runs its publish
    step the engine is also retrying whatever earlier providers left behind.
    A recorder that only answered LinkedIn refused those, and the run reported
    "the application called endpoints this test does not answer" naming
    Google Business - which was true, and nothing to do with LinkedIn.

    Answering every platform is not a loosening: each provider's own step
    still asserts on ITS traffic, and an endpoint no provider declares is
    still recorded as unpredicted. This provider's rows are inserted first so
    that where two platforms share a path suffix - "/me" belongs to three of
    them - the one under test wins.
    """
    every_platform = {suffix: (spec["platform"], responder) for suffix, responder in spec["endpoints"].items()}
    for name, other in PLATFORMS.items():
        for suffix, responder in other["endpoints"].items():
            every_platform.setdefault(suffix, (name, responder))
    return platforms(every_platform)


def connect_the_channel(page, journey, spec):
    """From the connect screen to an installed channel."""
    # THE PANEL HAS TO GO FIRST. Its first row is called "Connect social
    # accounts", role names match by substring, and it comes first - so
    # anything reaching for "Connect" gets the checklist and is taken back to
    # the page it is already on. Three runs went that way before a highlight
    # and a screenshot said so.
    dismiss_the_onboarding_checklist(page, journey)

    # EXACTLY that name. With the whole board in play "Connect Instagram" also
    # names "Connect Instagram (Direct)", and the two LinkedIn cards differ
    # only by their whole string. Playwright refuses an ambiguous locator
    # rather than picking one. Naming the card at all is possible ONLY because
    # each carries an aria-label - without it every card announces "Connect".
    connect = page.get_by_role("button", name=f"Connect {spec['card']}", exact=True)
    connect.highlight()
    journey(page, "the-control-about-to-be-clicked")

    connect.click()
    # PHOTOGRAPHED IMMEDIATELY, before waiting for anything. Waiting for the
    # network to settle first would hide a page that changed and changed back.
    journey(page, "the-instant-connect-was-pressed")
    page.wait_for_load_state("networkidle")
    journey(page, "after-pressing-connect")

    # SOME PLATFORMS ASK FOR SOMETHING FIRST, and pressing Connect only opens
    # a form on our own screen: Mastodon wants the instance to go to, Bluesky
    # a handle and an app password, DEV.to an API key. Each field is filled by
    # the name printed above it - which is also what a screen reader would
    # announce, so a field that cannot be found this way is a finding.
    for label, value in spec.get("typed_in", {}).items():
        page.get_by_label(label).fill(value)
    if spec.get("typed_in"):
        journey(page, "the-connection-details-are-typed-in")
        hand_them_over = page.get_by_role("button", name=spec["submitted_with"])
        expect(hand_them_over).to_be_enabled()
        hand_them_over.click()
        page.wait_for_load_state("networkidle")
        journey(page, "after-handing-over-the-connection-details")

    finish_installing(page, journey)


def open_the_composer(page, journey):
    """Take a person from wherever they are to a blank post.

    "New" opens a small menu offering "Post - Publish content to a channel"
    and "Idea - Capture a content idea"; the picture is what said so.
    """
    start = page.get_by_role("button", name="New").or_(page.get_by_role("link", name="New")).first
    start.click()
    # The menu FADES IN, so a shot taken the instant after the click catches it
    # half-drawn - which is how an earlier version concluded, wrongly, that
    # pressing "New" did nothing at all.
    page.wait_for_timeout(500)
    journey(page, "the-new-menu")

    # Named by the SENTENCE rather than the word "Post", because role names
    # match by substring and the screen behind carries an "All Posts" filter -
    # asking for "Post" is asking for both.
    write_a_post = (
        page.get_by_role("link", name="Publish content to a channel")
        .or_(page.get_by_role("button", name="Publish content to a channel"))
        .first
    )
    write_a_post.click()
    page.wait_for_load_state("networkidle")
    journey(page, "the-composer")


def choose_the_channel(page, journey, spec):
    """Tick the connected channel, which no post can skip.

    ACCESSIBILITY FINDING, and this is how it was found. Asking for the
    tick-box by the channel's name timed out; asking for tick-boxes AT ALL
    answers ZERO. The square drawn beside the channel is not a checkbox: it
    has no checkbox role, so nothing exposes whether it is ticked, and it
    carries no accessible name either.

    That is not cosmetic. Choosing a channel is the one step no post can skip,
    so as it stands a person using a screen reader cannot publish at all.

    Until the product names it, this presses what a sighted person presses:
    the pill itself, scoped to the MAIN landmark because the sidebar lists the
    same channel by the same name. A landmark is what the page offers for
    exactly this; it is not reaching into the DOM for a structure.
    """
    channel = page.get_by_role("main").get_by_text(ACCOUNT_ON_THE_PLATFORM, exact=True).first
    channel.highlight()
    journey(page, "the-channel-about-to-be-chosen")

    channel.click()
    journey(page, "the-channel-is-chosen")

    settle_what_the_platform_insists_on(page, journey, spec)


def settle_what_the_platform_insists_on(page, journey, spec):
    """Fill the fields a platform will not let a post exist without.

    TikTok grows its own panel the moment its channel is ticked, and one field
    in it - "Who can see this post" - is REQUIRED, populated live from
    TikTok's creator_info endpoint because the allowed audiences depend on the
    app's audit status. Leaving it empty stops even SAVING A DRAFT: pressing
    Save Draft produced the browser's own "Please fill out this field" bubble
    and nothing was kept.

    That is the product enforcing TikTok's rules, so the test answers it the
    way a person does rather than treating the refusal as a fault.
    """
    if spec["platform"] == "pinterest":
        # A PIN HAS TO GO ON A BOARD. Pinterest's panel carries a required
        # "SELECT BOARD" - a real <select>, filled from the boards its API
        # reports - and without it the browser refuses with "Please select an
        # item in the list", so not even a draft can be kept. The board on
        # offer is the one this suite's Pinterest endpoint answers with.
        board = page.get_by_role("main").get_by_role("combobox").filter(has_text="Select a board").first
        board.select_option(index=1)
        journey(page, "the-pinterest-board-is-chosen")
        return

    if spec["platform"] != "tiktok":
        return

    # ANOTHER ACCESSIBILITY FINDING, found the same way as the channel
    # tick-box: asking for this control BY ITS LABEL - "Who can see this
    # post", which is printed right above it and carries the required marker -
    # times out. The words are on the screen but are not tied to the field, so
    # nothing announces what this control is for.
    #
    # Addressed by role and by the wording of its own first option, which is
    # what a person reads inside the closed select.
    # A CUSTOM DROPDOWN, not a select - select_option times out on it. It
    # opens to "Everyone / Friends / Only you", which are exactly the three
    # audiences TikTok's creator_info was told to allow, so this is also proof
    # that the answer travelled from the platform boundary into the composer.
    audience = page.get_by_role("main").get_by_text("Select who can view this post").first
    audience.click()
    page.wait_for_timeout(500)
    journey(page, "the-tiktok-audience-choices")

    page.get_by_role("main").get_by_text("Everyone", exact=True).first.click()
    page.wait_for_timeout(500)
    journey(page, "the-tiktok-audience-is-chosen")


def let_the_worker_run(endpoints, platform, ticks=5):
    """Poll the publishing worker the way a deployment polls it: repeatedly.

    `run_publisher --once` is ONE tick, and a tick takes at most
    MAX_CONCURRENT_PUBLISHES posts, OLDEST FIRST. This suite deliberately
    shares one database across all thirteen platforms, so by the time the last
    one publishes there can be older due posts ahead of it - and the post just
    written, being the newest, is the one that falls off the end.

    That is the harness being unlike a deployment, which polls every fifteen
    seconds and drains the backlog, not the product refusing to publish. It
    showed up as YouTube - last alphabetically - passing on its own and
    failing in the full run, with the recorder showing the platform never
    called at all.

    Stops as soon as THIS platform's own endpoints have been called, so a
    healthy publish still costs exactly one tick.
    """
    for _ in range(ticks):
        before = endpoints.answered_by.count(platform)
        call_command("run_publisher", once=True)
        # HAND THE WORKER'S CONNECTIONS BACK after every tick: the engine
        # publishes on a thread pool, and Django closes those connections only
        # when the thread's storage is collected - far too late to drop a
        # database at the end of the run.
        connections.close_all()
        if endpoints.answered_by.count(platform) > before:
            return


def let_the_background_work_happen(seconds=3):
    """Run the OTHER worker - the one this suite has never run at all.

    A deployment runs TWO processes beside the web service: run_publisher, and
    process_tasks for django-background-tasks. Everything deferred belongs to
    the second one, and the publishing engine hands it the first comment -
    @background(schedule=...) ENQUEUES and nothing more.

    So a suite that runs only the publisher watches posts go out and every
    deferred job pile up unexecuted. requirements/background-work.md says it
    in the product's own words: "The web service only enqueues; nothing in it
    executes. A deployment with a healthy web service and no worker serves
    every page correctly and publishes nothing."

    BOUNDED BY --duration, because this command has no single-pass mode: left
    alone it runs for ever, and its exit code answers nothing - the same
    document records that it exits 0 against a database it cannot reach. So
    this asks for a few seconds and the caller reads the WORK afterwards,
    never the status.

    The sleep is shortened from its five-second default for the same reason
    the duration is short: an empty queue would otherwise cost five seconds
    per call, thirteen times a run, for nothing.
    """
    call_command("process_tasks", duration=seconds, sleep=0.5)
    # The tasks run in this process, so they publish through the same replaced
    # httpx.Client the recorder is behind - and they open their own database
    # connections, which are handed back here for the reason given above.
    connections.close_all()


def write_the_post(page, journey, spec, words):
    """Write the post, and title it where the platform demands a title.

    A DEV.TO POST IS AN ARTICLE, and everything that lists one afterwards -
    the chip on the calendar, the Queue, the Sent tab, the Drafts table -
    shows its TITLE and never its caption. Steps that looked for the words
    somebody typed into the caption box therefore found nothing at all, on a
    product behaving perfectly sensibly.

    So the title carries the same words as the post. That keeps one thing to
    look for instead of two, and it keeps each post distinguishable from the
    others - a fixed title would put the same words on all three.
    """
    if spec["platform"] == "devto":
        # THE TITLE GOES IN FIRST, which is also the order a person works in,
        # and here it is the only order that works at all:
        #
        #   ACCESSIBILITY FINDING. There IS a <label> reading "Article Title"
        #   on the page - it appears in the document's own list of labels -
        #   but get_by_label("Article Title") resolves to NOTHING, so that
        #   label is not tied to the field. A screen-reader user meets an
        #   unnamed text box, exactly as they do at the channel selector.
        #
        #   That leaves the placeholder, and the placeholder MOVES: it starts
        #   as an invitation to type a title and becomes THE CAPTION ITSELF
        #   once a caption exists - the document reported the title box
        #   holding "Fresh beans, brewed bright..." moments after that was
        #   typed below. So the field is findable before the caption is
        #   written and not after.
        page.get_by_placeholder("Enter a title").fill(words)
    page.get_by_placeholder("What would you like to share?").fill(words)
    journey(page, "the-post-is-written")


class TestOneProviderAllTheWay:
    """One provider, one person, one session - from sign-up to published.

    SERIAL BY CONSTRUCTION. The steps below run in the order they are written
    and share everything: the browser, the account, the connected channel and
    the recorder at the platform boundary. Each step therefore tests its own
    subject instead of re-testing the preamble, and the filmstrip in
    .e2e-screens/<provider>/ reads as one continuous story rather than a dozen
    restarts.

    It also means an early failure fails what follows, which is honest: a
    person who cannot connect a channel cannot publish either.

    THE ORDER IS THE ORDER THEY ARE WRITTEN IN, and the numbers in the names
    only say so out loud. What actually reorders steps is pytest regrouping
    items that share higher-scoped fixtures - that is what put the channel
    connection ahead of the navigation to the connect screen, so it hunted for
    a platform card on the publishing queue and waited thirty seconds for one.
    The autouse fixture below removes the difference it was sorting on.

    So a new step goes in its numbered PLACE, not merely under a numbered
    name: adding test_08 above test_07 ran it first, and step 07 then opened
    on a drafts list with no composer in sight.
    """

    @pytest.fixture(scope="class", autouse=True)
    def _every_step_declares_the_same_context(self, live_server, a_page, a_journey, endpoints, spec):
        """Give every step an IDENTICAL set of fixtures, so none can be moved.

        Numbering the steps was not enough on its own, and the collection
        order said so: 01, 03, 02, 04. pytest reorders items to group the ones
        that share higher-scoped fixtures, and the steps did not share them -
        two asked for `live_server` and `endpoints`, two did not, and the odd
        ones out were shuffled past their neighbours.

        An autouse fixture is added to EVERY step's fixture set, so after this
        there is no difference left to sort on and the numbers decide.
        """

    def test_01_a_person_signs_up(self, live_server, a_page, a_journey, spec):
        """Sign up, and look at whatever the product does next.

        Where this lands decides everything after it: signing up turns out to
        create the organization AND a workspace and to land on the publishing
        queue, so no fixture has to invent them.
        """
        sign_up(a_page, live_server, a_journey, f"e2e-{spec['platform']}@brightbean.test")

        assert live_server.url in a_page.url
        assert "Create your account" not in a_page.content(), (
            "still on the sign-up form after submitting it - the account was not created"
        )

    def test_02_is_offered_somewhere_to_publish(self, a_page, a_journey):
        """An account with no channels offers its own way out.

        A LINK, not a button, despite looking like one - addressed as the role
        it actually has, because Playwright's role selector is strict and the
        first attempt at this waited for a button that is not there.
        """
        a_page.get_by_role("link", name="Connect a Channel").click()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "connect-a-platform")

        assert a_page.get_by_role("button", name="Connect").count() > 0, (
            "the connect screen offers nothing to connect - every platform is "
            "probably rendered disabled because this deployment holds no app "
            "credentials for any of them"
        )

    def test_03_connects_the_channel(self, live_server, a_page, a_journey, endpoints, spec):
        """Install this provider, against its own endpoints.

        The properties asserted hold for every OAuth provider, and each can
        fail silently in production: the browser is sent to THE PLATFORM'S OWN
        authorisation page carrying this deployment's client id, a
        redirect_uri pointing back at us and a state; and the code that comes
        back is EXCHANGED.
        """
        connect_the_channel(a_page, a_journey, spec)

        assert endpoints.unexpected == [], f"unpredicted platform calls: {endpoints.unexpected}"

        # AND THIS PLATFORM'S OWN ENDPOINTS DID THE ANSWERING, not another's.
        # The recorder answers EVERY platform's table, because the worker
        # drains whatever earlier providers left behind - and it matches on
        # the TAIL of a path, so a suffix belongs to whoever claimed it first.
        # DEV.to installed perfectly against Threads' profile stub that way,
        # green banner and all, with an endpoint table of its own that was
        # EMPTY. A platform is covered only when its own routes were called.
        assert spec["platform"] in endpoints.answered_by, (
            "this platform's own endpoints were never called - it was satisfied by "
            f"{sorted(set(endpoints.answered_by)) or 'nothing'}, so nothing here is evidence about it."
            f"{endpoints.what_happened()}"
        )

        if spec.get("sends_you_to_the_platform", True):
            assert endpoints.navigations, f"the browser was never sent to the platform.{endpoints.what_happened()}"
            authorising = parse_qs(urlsplit(endpoints.navigations[0]).query)
            assert authorising["redirect_uri"][0].startswith(live_server.url), (
                f"the platform was told to send the person to {authorising['redirect_uri'][0]}, not back to us"
            )
            assert authorising.get("state", [""])[0], (
                "no state was carried, so the callback cannot be tied to this request"
            )
            assert authorising.get("client_id") or authorising.get("client_key"), (
                "the authorisation request identified no application"
            )

            # NOT VACUOUS. "no unexpected calls" is trivially true when no call
            # was made at all, which is exactly what the first version of this
            # reported: it passed while the browser sat doing nothing.
            assert endpoints.exchanged_a_token(), (
                f"the authorization code was never exchanged.{endpoints.what_happened()}"
                f"\n  this tab is at: {a_page.url}"
            )
        else:
            # NOBODY IS SENT ANYWHERE for these two, so what must not be
            # skipped is the CHECK. A person pastes a credential they made on
            # the platform, and the application has to go and ask the platform
            # whether it is any good. Storing it unasked would leave somebody
            # believing a channel is connected until their first post fails
            # silently, which is the worst moment to find out.
            assert endpoints.requests, (
                f"the details typed in were never checked against the platform.{endpoints.what_happened()}"
            )
            the_secret = list(spec["typed_in"].values())[-1]
            presented = [
                str(request.url)
                for request in endpoints.requests
                if the_secret in (headers_of(request) + endpoints.body_of(request))
            ]
            assert presented, (
                "the platform was called, but never given the credential the person typed in - "
                "so whatever was checked, it was not what they pasted."
                f"{endpoints.what_happened()}"
            )

        # INSTALLED, as the product itself reports it - not a row read out of
        # the database.
        expect(a_page.get_by_text(ACCOUNT_ON_THE_PLATFORM).first).to_be_visible()
        a_journey(a_page, "the-channel-is-installed")

    def test_04_writes_a_text_post_and_queues_it(self, a_page, a_journey, spec):
        """The first feature: a plain text post, written and queued."""
        if spec["platform"] in NEEDS_MEDIA:
            pytest.skip(
                f"{spec['card']} does not take a post of words alone in this product - "
                "its media journey is a feature step still to be written"
            )

        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey, spec)

        write_the_post(a_page, a_journey, spec, A_POST_ABOUT_COFFEE)

        # CHOOSING A CHANNEL CHANGES THE SCREEN, which the pictures show: the
        # counter becomes the platform's own caption limit, a "Customize"
        # control and a FIRST COMMENT field appear, the preview reports "1
        # platform", and "Add to Queue" goes from pale to solid.
        #
        # That last one is asserted rather than admired: Playwright will not
        # click a disabled control, so requiring it to be enabled states the
        # rule the product enforces instead of hoping the click lands.
        queue_it = a_page.get_by_role("button", name="Add to Queue")
        expect(queue_it).to_be_enabled()
        queue_it.click()

        # WAIT FOR THE COMPOSER TO LEAVE before looking for the post.
        #
        # This step was GREEN while queueing nothing. Queueing is a round trip
        # followed by a navigation to the calendar, and the screenshot taken
        # straight after the click showed the composer still sitting there
        # saying "Not saved yet", the channel count still 0 - while the
        # assertion below passed anyway, because the words it looks for were
        # still in the CAPTION BOX. An assertion that the subject of the test
        # cannot falsify is not an assertion.
        expect(queue_it).to_have_count(0)
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-post-is-queued")

        # QUEUED, as the product shows it: the press answers 204 and lands on
        # the calendar, where the post is a chip on the next available slot
        # carrying the opening words and the channel's badge.
        #
        # Matched on the opening words because the chip TRUNCATES - asserting
        # the whole sentence would fail against a product behaving correctly.
        expect(a_page.get_by_role("main").get_by_text(THE_OPENING_WORDS).first).to_be_visible()

    def test_05_publishes_a_post_now_and_it_reaches_the_platform(self, a_page, a_journey, endpoints, spec):
        """THE POINT OF ALL OF IT: a post the person publishes reaches the platform.

        Queueing alone proves nothing about publishing. It puts the post in
        the next free slot - tomorrow - so it is never due, the worker never
        picks it up, and the platform never hears a word. Everything up to
        here was preamble to this step.

        The product's own scheduling control offers "Now - Publish
        immediately" alongside "Next Available", "Prioritise" and "Set Date
        and Time"; the screenshot of that menu is what said so. This takes the
        one that publishes.
        """
        if spec["platform"] in NEEDS_MEDIA:
            pytest.skip(
                f"{spec['card']} does not take a post of words alone in this product - "
                "its media journey is a feature step still to be written"
            )

        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey, spec)
        write_the_post(a_page, a_journey, spec, A_POST_TO_PUBLISH_NOW)

        # THE FIRST COMMENT, WHERE THE PRODUCT OFFERS ONE. The field appears
        # beside the caption only for the platforms whose accounts report they
        # support it, so its PRESENCE is the product telling us whether this
        # post can carry one - which is why that is what is asked, rather than
        # a list of platform names kept in this file and going stale.
        the_first_comment = a_page.get_by_placeholder("First comment posted after the main post")
        this_platform_takes_a_first_comment = the_first_comment.count() > 0
        if this_platform_takes_a_first_comment:
            the_first_comment.fill(A_FIRST_COMMENT)
            a_journey(a_page, "the-first-comment-is-written")

        when = a_page.get_by_role("button", name="Next available")
        when.click()
        a_page.wait_for_timeout(500)
        a_journey(a_page, "the-scheduling-choices")

        a_page.get_by_text("Publish immediately").click()
        a_page.wait_for_timeout(500)
        a_journey(a_page, "set-to-publish-now")

        # The action button renames itself once "Now" is chosen, so it is
        # addressed by what it says rather than by what it said before.
        publish = a_page.get_by_role("button", name="Publish Now")
        expect(publish).to_be_enabled()
        publish.click()
        expect(publish).to_have_count(0)
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-post-was-handed-to-the-worker")

        # RUN THE WORKER, the way a deployment runs it.
        #
        # Pressing publish does not send anything: it hands the post to the
        # publishing engine, and the engine only acts when its worker polls.
        # The first version of this step asserted straight after the click and
        # failed with the recorder showing only the token exchange and the
        # account listing - which is the truth, and the reason queueing alone
        # could never have proved publishing works.
        #
        # `run_publisher --once` is the product's own worker running a single
        # poll cycle: the same entry point a deployment runs, not a reach into
        # the engine.
        let_the_worker_run(endpoints, spec["platform"])

        # AND THEN THE OTHER WORKER, which is what a deployment has running
        # beside the publisher. The engine does not post the first comment
        # itself - it enqueues it - so without this the comment is scheduled
        # on every publish and sent on none.
        let_the_background_work_happen()

        a_page.reload()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-post-was-published")

        # WHAT THE PLATFORM ACTUALLY RECEIVED. Not a status in our own
        # database - the caption has to appear in a request that left this
        # application for the platform's endpoint. Anything less would pass
        # against a product that marks posts published and sends nothing.
        # DECODED, because not every platform is sent JSON. Threads and
        # Mastodon post form-encoded bodies, where the caption arrives as
        # "Doors+open+at+seven" and a search for the plain words finds
        # nothing - which reported "nothing ever reached the platform" for a
        # publish that had gone out perfectly well.
        went_out = [
            f"{request.method} {request.url}"
            for request in endpoints.requests
            if A_POST_TO_PUBLISH_NOW[:11] in unquote_plus(endpoints.body_of(request))
        ]
        assert went_out, f"nothing carrying the post's text ever reached the platform.{endpoints.what_happened()}"

        # AND THE PLATFORM ANSWERED IT. `went_out` records what the
        # application SENT, including requests the recorder refused because
        # this test never predicted them - so on its own it is satisfied by a
        # publish that failed. That is not a hypothetical: the publish
        # endpoint was missing from the table above, every attempt got a 404,
        # and this step still reported the post as having reached the
        # platform.
        assert endpoints.unexpected == [], (
            f"the application called endpoints this test does not answer, so the publish failed: {endpoints.unexpected}"
        )

        # AND THE FIRST COMMENT FOLLOWED IT, as a SEPARATE request made by a
        # SEPARATE worker. Nothing in this suite had ever checked that, for
        # the plain reason that nothing had ever run the worker: the engine
        # enqueued the task on every publish and the queue was never drained.
        # A person who typed a first comment got a post and silence.
        if this_platform_takes_a_first_comment:
            the_comment_went_out = [
                f"{request.method} {request.url}"
                for request in endpoints.requests
                if A_FIRST_COMMENT[:14] in unquote_plus(endpoints.body_of(request))
            ]
            assert the_comment_went_out, (
                f"the post reached the platform but its first comment never did.{endpoints.what_happened()}"
            )

        # AND THE PRODUCT AGREES IT WENT OUT. The request reaching the platform
        # is one half; a person's evidence is the other. The publishing screen
        # keeps a "Sent" tab, so the post has to be findable there rather than
        # merely sitting on the calendar looking scheduled.
        # PROVE THE VIEW CHANGED BEFORE READING IT. The first attempt clicked
        # "List" and photographed the CALENDAR - the click had not taken
        # effect yet - and then asserted against that, which is why it
        # reported the post as still queued when it was looking at a calendar
        # that shows every post regardless of status.
        to_the_list = a_page.get_by_role("link", name="List").or_(a_page.get_by_role("button", name="List")).first
        to_the_list.highlight()
        a_journey(a_page, "the-control-that-opens-the-list")
        to_the_list.click()

        # The list is the view with the Queue / Drafts / Approvals / Sent tabs,
        # so waiting for a tab to exist is waiting for the view itself.
        sent = a_page.get_by_role("link", name="Sent").or_(a_page.get_by_role("button", name="Sent")).first
        expect(sent).to_be_visible()

        # A FULL LOAD OF THE LIST, not the client-side swap. The worker wrote
        # to the database from outside the browser's world, so a view that was
        # swapped in beforehand can be showing what was true a moment ago.
        # Reloading here removes that explanation, and leaves only the
        # product's own answer.
        a_page.reload()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-publishing-list")

        # THE QUEUE MUST HAVE LET IT GO. Asserting the post is "on the Sent
        # tab" is worthless on its own: the same words are visible on the
        # Queue tab, so the check passed while the screenshot showed both
        # posts still queued, each with a Publish button beside them. What
        # cannot be faked is the post LEAVING the queue.
        still_queued = a_page.get_by_role("main").get_by_text(A_POST_TO_PUBLISH_NOW[:11]).count()

        # SHOW WHAT IS ABOUT TO BE CLICKED, AND PROVE THE TAB CHANGED.
        #
        # An earlier version clicked "Sent", photographed, and read the counts
        # - and the picture showed the QUEUE tab still active, so both counts
        # came from the same view and the conclusion drawn from them ("it is
        # on Sent and on Queue") was worth nothing. The queue's own tab badge
        # is what settles which view is on screen.
        sent.highlight()
        a_journey(a_page, "the-control-that-opens-sent")
        sent.click()
        a_page.wait_for_load_state("networkidle")
        a_page.wait_for_timeout(1000)
        a_journey(a_page, "the-post-is-listed-as-sent")

        on_the_sent_tab = a_page.get_by_role("main").get_by_text(A_POST_TO_PUBLISH_NOW[:11]).count()

        # THE PRODUCT'S OWN ACCOUNT OF WHAT IT DID. The platform received the
        # post - that is asserted above and is not in doubt. What is in doubt
        # is whether the product knows, and these two counts say which defect
        # is present if the answer is no:
        #
        #   not on Sent  -> the publish was never recorded as one, and the
        #                   queue is right to still offer it;
        #   on Sent AND still on Queue -> it was recorded, and the queue is
        #                   over-inclusive - offering to publish, a second
        #                   time, something already sent.
        assert on_the_sent_tab, (
            "the platform received the post but the product does not list it as sent"
            f" (queue still shows it: {bool(still_queued)})"
        )
        assert not still_queued, (
            "the post is listed as sent AND still sitting in the queue with a"
            " Publish button beside it - pressing it would post it twice"
        )

    def test_06_attaches_a_picture(self, a_page, a_journey, spec, live_server):
        """Put a real file through the composer's own picker.

        This is the step the media-first platforms have been waiting for, and
        it starts where a person starts: the "Drag & drop or select files"
        well in the caption box. Playwright answers the operating system's
        file dialog with a file that exists on disk, so the browser uploads
        real bytes rather than the test poking a field.
        """
        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey, spec)

        # THE WORDS GO IN FIRST, AND THE DRAFT IS THEN WAITED FOR. Typing is
        # what sets the composer's autosave going, and autosave is what
        # creates the post - so the interesting moment for an upload is AFTER
        # the draft exists, while the page may not yet have been told its id.
        # Attaching before any of that happens is the easy path and proves
        # nothing.
        #
        # THE WAIT IS WHAT MAKES THIS A TEST. Autosave runs on a thirty-second
        # tick, so a step that types and attaches within a few seconds never
        # reaches that moment at all: the upload parks in the session with no
        # post to attach to, and is swept up later. Whether the hard path was
        # taken therefore depended on how slow the run happened to be - it
        # passed alone and failed at the tail of a full run, which is not a
        # test, it is a coin toss with a stopwatch.
        #
        # The product says when the draft exists, in its own words: the footer
        # reads "Not saved yet" until the first autosave answers and "Saved
        # HH:MM" afterwards. Waiting for that needs no internals.
        write_the_post(a_page, a_journey, spec, A_POST_WITH_A_PICTURE)

        if spec["platform"] == THE_PLATFORM_THAT_WAITS_FOR_THE_DRAFT:
            expect(a_page.get_by_text(re.compile(r"Saved \d\d:\d\d")).first).to_be_visible(timeout=60000)
            a_journey(a_page, "the-draft-exists-before-anything-is-attached")

        # WHAT THE BROWSER COULD NOT FETCH. The attachment appears and the
        # preview renders, but the thumbnail is drawn as a broken image - in
        # the composer AND in the platform preview. That is either the file we
        # sent, the URL the application serves it from, or the policy the page
        # runs under, and only the responses say which.
        refused = []
        a_page.on(
            "response",
            lambda response: refused.append(f"{response.status} {response.url}") if response.status >= 400 else None,
        )

        # WHAT THE BROWSER ITSELF PUT ON THE WIRE. The stored copy of every
        # picture this suite uploads is ZERO BYTES on disk - which is why the
        # thumbnail draws broken while its URL still answers 200, and why the
        # one platform that uploads bytes rather than a URL sends an empty
        # file. That is either the page failing to send the file or the
        # application failing to keep it, and nothing the product renders can
        # tell those apart. The request body can.
        with a_page.expect_file_chooser() as chosen:
            a_page.get_by_text("select files").first.click()

        # THE FILE IS HANDED OVER AS BYTES, NOT AS A PATH, and that is not a
        # detail. Naming the file on disk instead - set_files(str(path)) - was
        # measured, on this host, to store a file of ZERO BYTES: the thumbnail
        # drew broken in the composer and the preview, the picture's own URL
        # answered 200 with nothing in it, and LinkedIn, the one platform sent
        # the bytes rather than a URL, PUT an empty body and had a post built
        # on top of it. Handing the bytes over instead, and changing nothing
        # else, put the picture on the screen and the file on the wire.
        #
        # WHY the path form arrives empty is NOT established here. What is
        # established is that this suite reported a broken thumbnail and an
        # empty upload as defects of the product, and they were defects of
        # this line.
        attaching, its_type = what_this_platform_takes(spec)

        # WAIT FOR THE SERVER TO ANSWER THE UPLOAD, rather than for a fixed
        # number of milliseconds and a hope.
        #
        # THIS IS WHAT MADE THE VIDEO PLATFORMS FLAKY. set_files starts the
        # upload ASYNCHRONOUSLY, and what used to stand here was
        # wait_for_load_state("networkidle") followed by wait_for_timeout(1500).
        # networkidle can be satisfied BEFORE the request has even begun - the
        # page is already idle - so the entire guarantee was 1500ms.
        #
        # A picture fits in that. A VIDEO DOES NOT: it is a far bigger body to
        # send and to store. On a loaded machine the next step pressed
        # "Publish Now" a second or so later, and a video-only provider
        # refused the result - "TikTok only supports VIDEO posts", three
        # retries later. It only ever bit TikTok and YouTube, only on slow
        # runs, and every attempt to observe it added enough delay to make it
        # disappear.
        #
        # NOT because of server-side processing, which is worth saying because
        # it is the obvious guess and it is wrong: process_media_asset - the
        # background task that would run ffmpeg for the thumbnail and the
        # duration - is enqueued by the API, by MCP and by the media library,
        # and NOT by the composer's own upload. Nothing processes a file
        # attached here at all.
        #
        # WAITED FOR THE WAY A PERSON WAITS FOR IT: the thumbnail appears in
        # the strip, and only then is the post ready to send. The composer
        # renders that strip SERVER-SIDE and points it at the stored file, so
        # a thumbnail on the screen is the application saying it has the file.
        # There is nothing to watch on the wire that a person could not see.
        chosen.value.set_files(
            {
                "name": Path(attaching).name,
                "mimeType": its_type,
                "buffer": Path(attaching).read_bytes(),
            }
        )

        the_thumbnail = (
            a_page.get_by_role("main").locator("video").first
            if spec["platform"] in WANTS_VIDEO
            else a_page.get_by_role("main").get_by_role("img", name=Path(attaching).name).first
        )
        expect(the_thumbnail).to_be_visible(timeout=UPLOAD_PATIENCE)

        # AND IT DREW. Being on the page is not enough - videoWidth and
        # naturalWidth stay 0 until the browser has decoded a frame out of
        # what the application served, which is the difference between a
        # thumbnail and a thumbnail-shaped hole. This is also what makes the
        # wait honest: it ends when the file is really there.
        a_page.wait_for_function(
            "element => (element.videoWidth || element.naturalWidth || 0) > 0",
            arg=the_thumbnail.element_handle(),
            timeout=UPLOAD_PATIENCE,
        )

        a_journey(a_page, "the-picture-is-attached")

        # THE FILE IS ATTACHED, as the composer says: its name appears beside
        # a control for removing it again, and the platform preview lays out a
        # post card around it.
        # BY ROLE, not by the file's name. Matching the name passed only while
        # the thumbnail was BROKEN - the name is the image's alt text, which
        # the browser draws in place of a picture it could not load. An
        # assertion that holds only while something is broken is worse than
        # none: it would have started failing the day the thumbnail worked.
        # NOTHING WAS REFUSED AND THE POLICY BLOCKED NOTHING - FOR EVERY
        # PLATFORM, AND BEFORE THE BRANCH BELOW GETS TO RETURN.
        #
        # These two assertions used to sit AFTER the video branch's `return`,
        # so the only two platforms that take video - which are also the only
        # two that ever fail with "no media on the post" - were the two this
        # suite never checked for a refused upload. A blocked or failed POST
        # was invisible exactly where it mattered.
        #
        # The frame check below cannot stand in for this: a <video> decodes
        # perfectly well from a blob the page made for itself, so a picture on
        # the screen is not evidence that the file reached the server. That is
        # the same mistake as the old "an <img> is present" check, which
        # passed for as long as the thumbnail was broken.
        assert refused == [], f"the browser was refused: {refused}"
        assert a_page.evaluate("window.__cspViolations || []") == [], "the policy blocked something on this page"

        if spec["platform"] in WANTS_VIDEO:
            # A VIDEO IS NOT AN <img>, and it is not enough that one is on the
            # page either: videoWidth stays 0 until the browser has decoded a
            # FRAME, so this is the same distinction the picture check makes
            # between a file being there and a file being readable.
            the_video = a_page.get_by_role("main").locator("video").first
            the_video.wait_for(state="attached")
            a_page.wait_for_timeout(1500)
            assert the_video.evaluate("video => video.videoWidth") > 0, (
                "the attached video never produced a frame - the browser was given nothing it could decode"
            )
            a_journey(a_page, "the-post-with-a-video-is-ready")
            return

        assert a_page.get_by_role("main").get_by_role("img").count() >= 1, (
            "the post has no picture on it after attaching one"
        )

        a_journey(a_page, "the-post-with-a-picture-is-ready")

        # AND THE PICTURE ACTUALLY DREW. An <img> the browser could not decode
        # is still an <img>, so counting them passes on a broken thumbnail -
        # which is precisely what this step did for as long as the file behind
        # it was empty, while the screenshot showed a torn-paper icon and the
        # file's name in place of the photograph. naturalWidth stays 0 until
        # real bytes have been decoded, so this separates "a picture is on the
        # post" from "something shaped like a picture is on the post".
        the_picture = a_page.get_by_role("main").get_by_role("img", name=AN_IMAGE.name).first
        assert the_picture.evaluate("img => img.naturalWidth") > 0, (
            "the attached picture is drawn broken - the browser was given no image it could decode"
        )

    def test_07_publishes_the_picture_to_the_platform(self, a_page, a_journey, endpoints, spec):
        """Send the picture out, and check the platform was given one.

        This is what the media-first platforms exist for, and it is a
        different assertion from the text post: the request that leaves has to
        carry a URL for the FILE, not just words. Instagram in particular
        publishes in two moves - a container carrying the image, then a
        publish of that container - so a run that only reached the first would
        look successful and post nothing.
        """
        when = a_page.get_by_role("button", name="Next available")
        when.click()
        a_page.wait_for_timeout(500)
        a_page.get_by_text("Publish immediately").click()
        a_page.wait_for_timeout(500)
        a_journey(a_page, "the-picture-post-set-to-publish-now")

        publish = a_page.get_by_role("button", name="Publish Now")
        expect(publish).to_be_enabled()
        publish.click()
        expect(publish).to_have_count(0)
        a_page.wait_for_load_state("networkidle")

        let_the_worker_run(endpoints, spec["platform"])

        a_page.reload()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-picture-post-was-published")

        # OPEN THE POST AND LOOK AT IT. The chip on the calendar is the
        # product's own record of what it tried to send; opening it shows the
        # channel, the title, and - the thing that decides this - whether any
        # media is actually attached to the post the worker published. Reading
        # that off the screen beats inferring it from an engine log.
        the_post = a_page.get_by_role("main").get_by_role("link", name=re.compile(A_POST_WITH_A_PICTURE[:10])).first
        if the_post.count():
            the_post.click()
            a_page.wait_for_load_state("networkidle")
            a_page.wait_for_timeout(1500)
            a_journey(a_page, "the-post-as-the-product-has-it")
            a_page.go_back()
            a_page.wait_for_load_state("networkidle")

        # THE FILE'S OWN NAME had to travel. The caption could reach a platform
        # with no picture attached at all; the uploaded file's name in the
        # request is what says the picture went with it.
        # TWO WAYS TO HAND A PLATFORM A PICTURE, and this has to accept both.
        # Facebook and Instagram are given a URL to fetch, so the file's name
        # appears in the request. LinkedIn is given the BYTES: it asks where to
        # put them, PUTs them there and then refers to the result by URN, so
        # the name appears nowhere at all and a check for it reported "the
        # platform was never given the picture" about a publish that had just
        # uploaded the entire file.
        # WHAT THIS PLATFORM WAS SENT depends on what it takes: a picture for
        # most, a video for the two that refuse pictures. Both are recognised
        # the same two ways - by the file's name, which appears when the
        # platform is handed a URL to fetch, and by the file's own signature,
        # which appears when it is handed the bytes.
        the_file = A_VIDEO if spec["platform"] in WANTS_VIDEO else AN_IMAGE
        # PNG says so in its first four bytes; MP4 carries "ftyp" a few bytes
        # in, so neither is looked for at any fixed position.
        its_signature = b"ftyp" if spec["platform"] in WANTS_VIDEO else b"\x89PNG"
        carried_the_picture = [
            f"{request.method} {request.url}"
            for request, sent in zip(endpoints.requests, endpoints.sent, strict=False)
            if the_file.stem in unquote_plus(sent.decode(errors="replace") + str(request.url))
            # ANYWHERE IN THE BODY, not only at its start. Mastodon is sent
            # the file as one part of a MULTIPART body, so the signature sits
            # a couple of hundred bytes in, behind the part's own headers -
            # and the name in those headers is the engine's temp file, not
            # ours, so neither the name nor the first four bytes match while
            # 2514 bytes of picture go up perfectly well.
            or its_signature in sent
        ]
        assert carried_the_picture, (
            "the platform was never given the picture."
            f"{endpoints.what_happened()}"
            # WHICH BRANCH THE PROVIDER TOOK, which is the whole question when
            # a byte-uploading platform sends nothing. httpx.Client is replaced
            # process-wide here, so a provider that reaches for the media's URL
            # instead of its local copy has that fetch answered by THIS
            # recorder - as a 404, recorded below. Naming it separates "the
            # engine's temp copy was empty" from "the provider went looking for
            # a URL we do not serve to it".
            f"\n  refused as unpredicted: {endpoints.unexpected or 'nothing'}"
            f"\n  bodies seen: "
            f"{[(str(r.url)[-40:], len(s), s[:8]) for r, s in zip(endpoints.requests, endpoints.sent, strict=False)]}"
        )

        assert endpoints.unexpected == [], (
            f"the application called endpoints this test does not answer, so the publish failed: {endpoints.unexpected}"
        )

    def test_08_saves_a_draft_and_finds_it_under_drafts(self, a_page, a_journey, spec):
        """A post that is not ready to go anywhere yet.

        The composer offers "Save Draft" beside the publishing controls, and
        the publishing screen keeps a Drafts tab. Nothing in this suite had
        pressed either, so drafts could have gone nowhere at all and every
        test would still have passed.
        """
        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey, spec)
        write_the_post(a_page, a_journey, spec, A_POST_LEFT_AS_A_DRAFT)

        save = a_page.get_by_role("button", name="Save Draft")
        expect(save).to_be_enabled()
        save.click()
        a_page.wait_for_timeout(1500)
        a_journey(a_page, "the-instant-save-draft-was-pressed")

        # The composer leaves once the draft is kept, exactly as it does when
        # a post is queued - waiting for it to go is what makes the assertion
        # below about the DRAFTS tab rather than about the caption box it was
        # typed into.
        expect(save).to_have_count(0)
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-draft-was-saved")

        to_the_list = a_page.get_by_role("link", name="List").or_(a_page.get_by_role("button", name="List")).first
        to_the_list.click()
        drafts = a_page.get_by_role("link", name="Drafts").or_(a_page.get_by_role("button", name="Drafts")).first
        expect(drafts).to_be_visible()
        drafts.click()
        a_page.wait_for_load_state("networkidle")
        a_page.wait_for_timeout(1000)
        a_journey(a_page, "the-draft-is-listed")

        expect(a_page.get_by_role("main").get_by_text(A_POST_LEFT_AS_A_DRAFT[:12]).first).to_be_visible()

    def test_09_an_article_with_no_title_is_refused_before_it_is_queued(self, a_page, a_journey, spec):
        """A post that CANNOT publish must not be accepted as though it will.

        DEV.to will not take an article without a title, and this composer used
        to accept one anyway: it queued, it scheduled, and the publishing
        engine refused it three retries later - "DEV.to requires a title. Set
        the post title before publishing." - with nobody watching. The person
        was told at the one moment they could do nothing about it.

        So the refusal belongs here, while the composer is still open, and this
        is the step that says so. It fails against the product as it was.
        """
        if spec["platform"] != "devto":
            pytest.skip("only DEV.to demands a title of every post; the others have nothing to withhold")

        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey, spec)

        # THE TITLE IS LEFT EMPTY ON PURPOSE. That is the whole subject.
        a_page.get_by_placeholder("What would you like to share?").fill(AN_ARTICLE_NOBODY_TITLED)
        a_journey(a_page, "an-article-nobody-titled")

        queue_it = a_page.get_by_role("button", name="Add to Queue")
        expect(queue_it).to_be_enabled()
        queue_it.click()
        a_page.wait_for_timeout(1500)
        a_journey(a_page, "what-the-composer-says-about-the-missing-title")

        # THE PERSON IS TOLD, in words, and is still standing in the composer
        # with the post in front of them rather than on a calendar somewhere.
        expect(a_page.get_by_text("Give the article a title").first).to_be_visible()
        expect(queue_it).to_be_visible()

        # AND NOTHING WAS QUEUED. The message alone is not enough: what made
        # this a defect was the post going into the queue regardless.
        # LEAVING THE COMPOSER IS A STEP OF ITS OWN. "List" belongs to the
        # publishing screen, and we are still standing in the composer -
        # which is the point of the assertion above.
        a_page.get_by_role("link", name="Publish").first.click()
        a_page.wait_for_load_state("networkidle")
        to_the_list = a_page.get_by_role("link", name="List").or_(a_page.get_by_role("button", name="List")).first
        to_the_list.click()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-queue-after-the-refusal")
        expect(a_page.get_by_role("main").get_by_text(AN_ARTICLE_NOBODY_TITLED[:14])).to_have_count(0)
