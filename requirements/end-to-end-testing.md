# Behaviour is verified end-to-end, in a browser, against a real database

A feature is done when it is tested end-to-end.

The suite drives a real browser against the application served from a real PostgreSQL database, under the same Content Security Policy production enforces.

**The database is ephemeral and belongs to the run.** It is created when the session starts and destroyed when it ends. A test run must not require a database to have been provisioned first, must not need credentials, and must not leave anything behind.

**The e2e settings enforce what production enforces.** Any setting that is relaxed for local convenience - the CSP is the one that matters - is restored here. A harness configured more leniently than production reports success for defects that only production will meet.

**Unit tests are not a substitute.** The defect that motivated this requirement passed the ENTIRE unit suite, every static check, and `manage.py check`, because it only manifests when a browser applies a policy to a rendered page.

No test count is quoted here, deliberately. A number written into prose is a claim that stops being true the next time somebody adds a test, and this sentence had already outlived two of them. If you want the figure, run the suite - it is the only source that cannot be stale.

## End-to-end starts at the front door, not at the database

**A test that builds its rows through the ORM and then runs the worker is not an end-to-end test.** It verifies the half of the chain below the database. The composer - the screen a person actually uses, which decides what lands in those rows - never runs, so a composer that drops a link, writes a caption into the wrong field or fails to attach the second image passes every such test.

A test earns the name by starting where a person starts: open the application, sign up or sign in, and work forward through the screens using only what is on them.

## Tests are written from the outside, and may not read the internals

A browser test written from `urls.py`, the view functions or the form field names is the same test with a browser bolted on. It proves the fields somebody was told about still work; it cannot notice that the control which submits them is covered, disabled, off-screen or never rendered.

So: **no URL names, no view internals, no form field names, no model layout.** Navigate by the links and buttons that are visible, and select controls by their role and their visible name - what a person reads. If something cannot be found and operated from the rendered page, that is a finding about the product, not a licence to reach into the code for a selector.

## The authoring procedure: drive, screenshot, LOOK, then select

Write these tests by looking at the application, not by imagining it.

1. Drive the browser to the screen.
2. Take a full-page screenshot.
3. **Look at the screenshot.** Actually read it back and see what is on the page.
4. Choose the control you can see, and address it by role and visible name.

Step 3 is the one that gets skipped, and skipping it produces a test that asserts a page nobody rendered. This procedure has already paid for itself twice in one sitting: the landing page's "Connect a Channel" call to action turns out to be a LINK that merely looks like a button, and the connect screen turns out to show every OAuth platform greyed out as "Not Configured" - neither of which was guessable from the templates.

## Interactability is Playwright's job, not a pixel comparison

Playwright's actionability checks are the guarantee: before it clicks, it waits for the element to be visible, enabled, stable, and actually receiving pointer events, and fails if the element is covered by an overlay or has no size. That is a stronger and far less brittle statement than an image diff.

**Visual pixel comparison is deliberately NOT used.** Screenshots are an authoring aid for the person writing the test, not an assertion. A baseline image is specific to the platform that rendered it - font rasterisation and antialiasing differ enough between a developer's machine and the Linux runner that such a test would be green locally and meaningless in CI.

## The compiled front-end assets are part of the harness

**The browser suite is invalid without them.** Tailwind's stylesheet is compiled, not committed, and the collected static files are what the application serves. Run the suite without them and every page renders unstyled: the layout is not the one that ships, elements sit in places they never occupy in production, and the actionability checks above then pass against something no customer ever sees.

Build them before the browser runs:

    cd theme/static_src && npm ci && npm run build
    python manage.py collectstatic --noinput

This applies wherever the suite runs, CI included.

## Only the outbound wire is substituted

Everything inside the application is real: the browser, the pages, the forms, the database, the worker command, the scheduling, the publishing engine, credential resolution, and each provider's own request building and response parsing.

The one substitution is the wire to the social platforms, because those platforms do not exist for a test run. Requests are answered by an in-process transport, and a request the test did not predict is recorded and refused rather than quietly satisfied - so a provider reaching for an endpoint nobody expected shows up instead of passing silently.

## Connecting a channel is the current limit, and it is a product fact

Publishing cannot be reached until a channel is connected, and the connect screen offers only what the deployment has been configured for. With no platform credentials in the environment, every OAuth platform renders disabled and marked "Not Configured"; the channels a person can connect by typing something into a form - Bluesky's app password, Mastodon's instance - are the ones a test can drive today without further work.

Covering an OAuth platform end-to-end therefore needs two things stated up front: credentials configured so the connect screen offers the platform at all, and the redirect to the platform's own authorisation page intercepted, since it is not reachable from a test run.
