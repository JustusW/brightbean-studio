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
from playwright.sync_api import expect

#: The person this suite plays. A real address is never sent anywhere - the
#: e2e settings keep mail in memory - but it has to look like one.
EMAIL = "e2e-composer@brightbean.test"
PASSWORD = "compose-a-post-8"


def sign_up(page, live_server, journey):
    """Create an account through the sign-up form, as a new customer would.

    Labelled controls only. If the email field stops being labelled "Email",
    or the button stops saying "Create Account", this breaks - which is the
    intent: those are the things a person reads.
    """
    page.goto(live_server.url, wait_until="domcontentloaded")
    journey(page, "front-door")

    page.get_by_role("link", name="Sign up").click()
    page.wait_for_load_state("domcontentloaded")
    journey(page, "sign-up-form")

    page.get_by_label("Email").fill(EMAIL)
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


@pytest.mark.django_db(transaction=True)
def test_a_new_customer_can_create_an_account_and_gets_somewhere(live_server, page, journey):
    """Sign up, and look at whatever the product does next.

    Where this lands decides everything after it, and it is not something to
    assume: signing up turns out to create the organization AND a workspace
    and to land on the publishing queue, so no fixture has to invent them.
    """
    sign_up(page, live_server, journey)

    assert live_server.url in page.url
    assert "Create your account" not in page.content(), (
        "still on the sign-up form after submitting it - the account was not created"
    )


@pytest.mark.django_db(transaction=True)
def test_a_new_customer_is_offered_a_way_to_connect_a_channel(live_server, page, journey):
    """Signing up leaves an account with nowhere to publish. Follow the offer.

    The landing page's own call to action is "Connect a Channel", and the
    checklist's first item is "Connect social accounts", so a person's next
    move is not in doubt.
    """
    sign_up(page, live_server, journey)

    # A LINK, not a button, despite looking like one. Addressed as the role it
    # actually has: Playwright's role selector is strict, and the first
    # attempt at this timed out waiting for a button that is not there.
    page.get_by_role("link", name="Connect a Channel").click()
    page.wait_for_load_state("networkidle")
    journey(page, "connect-a-platform")

    assert page.get_by_role("link", name="Connect").count() > 0, (
        "the connect screen offers nothing to connect - every platform is "
        "probably rendered disabled because this deployment holds no app "
        "credentials for any of them"
    )


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

    def what_happened(self):
        """Everything the platform boundary saw, for a failure message."""
        return (
            f"\n  browser was sent to: {self.navigations or 'nothing'}"
            f"\n  server called: {[str(r.url) for r in self.requests] or 'nothing'}"
        )


@pytest.fixture
def platforms(page, monkeypatch):
    """Install both halves of the platform boundary and return the recorder."""

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

        monkeypatch.setattr(httpx, "Client", client_with_mock_transport)
        return endpoints

    return install


@pytest.mark.django_db(transaction=True)
def test_a_person_can_connect_facebook_through_the_connect_screen(live_server, page, journey, platforms):
    """Connect a channel the way a person does, against a mocked platform.

    The routes below are the endpoints Facebook itself would serve. Whatever
    is missing shows up as an unpredicted call rather than a silent pass, so
    this is also how the rest of the flow gets discovered.
    """
    endpoints = platforms(
        {
            "/oauth/access_token": lambda request: httpx.Response(
                200,
                json={"access_token": "e2e-user-token", "token_type": "bearer", "expires_in": 5184000},
            ),
            "/me/accounts": lambda request: httpx.Response(
                200,
                json={
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
                },
            ),
            "/me": lambda request: httpx.Response(
                200,
                json={"id": "e2e-user", "name": "Brightbean Tester", "picture": {"data": {"url": ""}}},
            ),
        }
    )

    sign_up(page, live_server, journey)
    page.get_by_role("link", name="Connect a Channel").click()
    page.wait_for_load_state("networkidle")
    journey(page, "connect-a-platform")

    # The browser's own report of what went wrong on the page. Not internals:
    # this is what the developer console shows anybody who opens it, and a
    # page whose scripts are failing is a fact about the page.
    complaints = []
    page.on("console", lambda message: complaints.append(f"{message.type}: {message.text}"))
    page.on("pageerror", lambda error: complaints.append(f"pageerror: {error}"))

    # SHOW ME WHAT I AM ABOUT TO CLICK, then photograph it.
    #
    # Role names match by SUBSTRING, and this page has more than one thing
    # containing "Connect" - the onboarding checklist offers a row called
    # "Connect social accounts" which leads back to this very page, so
    # clicking it looks exactly like nothing happening. Matching EXACTLY
    # "Connect" instead matches nothing at all, because the cards' control
    # carries a trailing arrow.
    #
    # So rather than guess a third time: highlight the candidate, take its
    # picture, and read the picture.
    dismiss_the_onboarding_checklist(page, journey)

    # Named for the platform it connects, and a BUTTON - each card submits a
    # form. Both facts came out of driving it: "Connect" alone named ten
    # controls identically, which is the accessibility defect this flow
    # uncovered, and the cards were never links at all.
    connect = page.get_by_role("button", name="Connect Facebook")
    connect.highlight()
    journey(page, "the-control-about-to-be-clicked")

    connect.click()
    # PHOTOGRAPHED IMMEDIATELY, before waiting for anything. Waiting for the
    # network to settle first would hide a page that changed and changed back.
    journey(page, "the-instant-connect-was-pressed")
    page.wait_for_load_state("networkidle")
    journey(page, "after-pressing-connect")

    assert endpoints.unexpected == [], f"unpredicted platform calls: {endpoints.unexpected}"
    # NOT VACUOUS. "no unexpected calls" is trivially true when no call was
    # made at all, which is exactly what the first version of this reported:
    # it passed while the browser sat on the connect screen having done
    # nothing. The flow is only real if the code was actually exchanged.
    assert endpoints.call("/oauth/access_token") is not None, (
        f"the authorization code was never exchanged.{endpoints.what_happened()}\n  this tab is at: {page.url}"
    )
