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

HOW TO EXTEND IT: drive the browser, screenshot, LOOK at the screenshot, and
pick the control you can see. Do not guess a selector from the template.
"""

from pathlib import Path

import pytest

#: Screenshots are an AUTHORING AID, not an artifact: they are what the person
#: writing a test reads back to see what is actually on the screen. They land
#: at the repository root and are ignored by git.
#:
#: Inside the repository on purpose. An earlier version wrote a level higher,
#: which is outside the checkout entirely when this runs in CI.
SCREENS = Path(__file__).resolve().parents[2] / ".e2e-screens"


def look(page, name):
    """Save a full-page screenshot to be read back and looked at."""
    SCREENS.mkdir(parents=True, exist_ok=True)
    path = SCREENS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


@pytest.mark.django_db(transaction=True)
def test_the_front_door_offers_a_way_in(live_server, page):
    """Open the application as an anonymous visitor and look at it.

    The first thing any person does. Whatever this shows is the starting
    point for composing a post; nothing below it may be assumed.
    """
    page.goto(live_server.url, wait_until="domcontentloaded")
    look(page, "01-front-door")

    assert page.title() != "", "the front door renders no title at all"


@pytest.mark.django_db(transaction=True)
def test_a_visitor_can_reach_the_sign_up_form(live_server, page):
    """Follow the link a person without an account would follow.

    The front door offers "Sign up". Nothing here knows where that goes - it
    is clicked as a link by its visible text, which is the only thing a person
    has to go on.
    """
    page.goto(live_server.url, wait_until="domcontentloaded")
    page.get_by_role("link", name="Sign up").click()
    page.wait_for_load_state("domcontentloaded")
    look(page, "02-sign-up")

    assert page.get_by_role("button").count() > 0, "the sign-up page offers nothing to press"


#: The person this suite plays. A real address is never sent anywhere - the
#: e2e settings keep mail in memory - but it has to look like one.
EMAIL = "e2e-composer@brightbean.test"
PASSWORD = "compose-a-post-8"


def sign_up(page, live_server):
    """Create an account through the sign-up form, as a new customer would.

    Labelled controls only. If the email field stops being labelled "Email",
    or the button stops saying "Create Account", this breaks - which is the
    intent: those are the things a person reads.
    """
    page.goto(live_server.url, wait_until="domcontentloaded")
    page.get_by_role("link", name="Sign up").click()
    page.wait_for_load_state("domcontentloaded")

    page.get_by_label("Email").fill(EMAIL)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Create Account").click()
    page.wait_for_load_state("networkidle")


@pytest.mark.django_db(transaction=True)
def test_a_new_customer_can_create_an_account_and_gets_somewhere(live_server, page):
    """Sign up, and look at whatever the product does next.

    Where this lands decides everything after it - an onboarding wizard, a
    workspace, an empty dashboard - and it is not something to assume.
    """
    sign_up(page, live_server)
    look(page, "03-after-sign-up")

    assert live_server.url in page.url
    assert "Create your account" not in page.content(), (
        "still on the sign-up form after submitting it - the account was not created"
    )


@pytest.mark.django_db(transaction=True)
def test_a_new_customer_is_offered_a_way_to_connect_a_channel(live_server, page):
    """Signing up leaves an account with nowhere to publish. Follow the offer.

    The landing page's own call to action is "Connect a Channel", and the
    checklist's first item is "Connect social accounts" - so a person's next
    move is not in doubt. What that opens decides how far a test can drive
    the real flow, since connecting is an OAuth round trip to a platform that
    does not exist in this harness.
    """
    sign_up(page, live_server)

    # A LINK, not a button, despite looking like one. Asserted as the role it
    # actually has: Playwright's role selector is strict, and the first
    # attempt at this timed out waiting for a button that is not there.
    page.get_by_role("link", name="Connect a Channel").click()
    page.wait_for_load_state("networkidle")
    look(page, "04-connect-a-channel")
