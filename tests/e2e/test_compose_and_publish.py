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
from urllib.parse import parse_qs, urlsplit

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
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.navigations: list[str] = []
        self.unexpected: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, responder in self.routes.items():
            if request.url.path.endswith(suffix):
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
            answered the page's own flatpickr and chart.js - which this
            application loads from a public CDN - with an OAuth redirect. That
            corrupts the page's scripts, including the date picker any
            scheduling test needs, while looking like a working test.

            An authorisation request is recognisable without knowing anything
            about this product: it is the one carrying a redirect_uri.

            Note what this leaves standing: the suite fetches those CDN assets
            over the real internet, so a browser run is not hermetic.
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


def answers(payload, status=200):
    """A platform endpoint that always answers the same document."""
    return lambda request: httpx.Response(status, json=payload)


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
OAUTH_PLATFORMS = {
    "instagram": {
        "card": "Instagram",
        "endpoints": {
            "/oauth/access_token": A_TOKEN,
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
        },
    },
    "youtube": {
        "card": "YouTube",
        "endpoints": {
            "/token": A_TOKEN,
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
            "/me": answers({"id": "e2e-user", "name": "Brightbean Tester", "picture": {"data": {"url": ""}}}),
        },
    },
}


@pytest.fixture(scope="class", params=sorted(OAUTH_PLATFORMS))
def spec(request):
    """The provider this run of the suite is about - one run per row."""
    return {"platform": request.param, **OAUTH_PLATFORMS[request.param]}


@pytest.fixture(scope="class")
def endpoints(platforms, spec):
    """The platform's own endpoints, answered for this provider's whole run."""
    return platforms(spec["endpoints"])


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


def choose_the_channel(page, journey):
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

    THE NUMBERS IN THE NAMES ARE LOAD-BEARING. Tests are collected in NAME
    order, not in the order they are written, and this suite was written
    assuming otherwise: the step that connects the channel sorted ahead of the
    step that navigates to the connect screen, so it hunted for a platform
    card on the publishing queue and waited thirty seconds for one. The
    screenshot of that moment shows the Publish page with the onboarding panel
    still open, which is what said so.

    Two digits, because the feature steps to come will pass nine.
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

        assert endpoints.navigations, f"the browser was never sent to the platform.{endpoints.what_happened()}"
        authorising = parse_qs(urlsplit(endpoints.navigations[0]).query)
        assert authorising["redirect_uri"][0].startswith(live_server.url), (
            f"the platform was told to send the person to {authorising['redirect_uri'][0]}, not back to us"
        )
        assert authorising.get("state", [""])[0], "no state was carried, so the callback cannot be tied to this request"
        assert authorising.get("client_id") or authorising.get("client_key"), (
            "the authorisation request identified no application"
        )

        # NOT VACUOUS. "no unexpected calls" is trivially true when no call was
        # made at all, which is exactly what the first version of this
        # reported: it passed while the browser sat doing nothing.
        assert endpoints.exchanged_a_token(), (
            f"the authorization code was never exchanged.{endpoints.what_happened()}"
            f"\n  this tab is at: {a_page.url}"
        )

        # INSTALLED, as the product itself reports it - not a row read out of
        # the database.
        expect(a_page.get_by_text(ACCOUNT_ON_THE_PLATFORM).first).to_be_visible()
        a_journey(a_page, "the-channel-is-installed")

    def test_04_writes_a_text_post_and_queues_it(self, a_page, a_journey):
        """The first feature: a plain text post, written and queued."""
        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey)

        a_page.get_by_placeholder("What would you like to share?").fill(A_POST_ABOUT_COFFEE)
        a_journey(a_page, "the-post-is-written")

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

    def test_05_publishes_a_post_now_and_it_reaches_the_platform(self, a_page, a_journey, endpoints):
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
        open_the_composer(a_page, a_journey)
        choose_the_channel(a_page, a_journey)
        a_page.get_by_placeholder("What would you like to share?").fill(A_POST_TO_PUBLISH_NOW)

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
        call_command("run_publisher", once=True)

        # HAND THE WORKER'S CONNECTIONS BACK. The engine publishes on a thread
        # pool and this call opens a connection of its own; Django closes them
        # when the thread's storage is collected, which is far too late. Left
        # alone, the session survives the test and dropping the database fails
        # with "1 andere Sitzung verwendet die Datenbank" - reported as a
        # PytestWarning, which is an error wearing a smaller hat.
        connections.close_all()

        a_page.reload()
        a_page.wait_for_load_state("networkidle")
        a_journey(a_page, "the-post-was-published")

        # WHAT THE PLATFORM ACTUALLY RECEIVED. Not a status in our own
        # database - the caption has to appear in a request that left this
        # application for the platform's endpoint. Anything less would pass
        # against a product that marks posts published and sends nothing.
        went_out = [
            f"{request.method} {request.url}"
            for request in endpoints.requests
            if A_POST_TO_PUBLISH_NOW[:11] in endpoints.body_of(request)
        ]
        assert went_out, (
            "nothing carrying the post's text ever reached the platform."
            f"{endpoints.what_happened()}"
        )

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
