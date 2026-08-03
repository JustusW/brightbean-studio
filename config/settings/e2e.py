"""Settings for the end-to-end suite: the test settings, with CSP ENFORCED.

That single difference is the reason this module exists.

config/settings/test.py runs the Content Security Policy in report-only mode.
Under report-only a violating inline script is reported to the console and then
executed anyway, so the page works and a broken policy is invisible. An e2e
suite that inherited those settings would pass happily against a tree in which
every inline script is refused in production.

Do not "simplify" this file by deleting the two lines that swap the policy back.
They are the entire point of it.
"""

import os

# CONFIGURE THE DEPLOYMENT FOR EVERY PLATFORM, BEFORE THE SETTINGS BELOW READ
# THE ENVIRONMENT.
#
# The connect screen offers only the platforms this deployment holds app
# credentials for; without them every OAuth platform renders greyed out and
# labelled "Not Configured", and nobody - person or test - can connect one.
# An end-to-end suite that cannot connect a channel cannot publish, so it
# would be reduced to asserting the half of the product below the database.
#
# These are placeholders and are meant to look like it. They are never sent
# anywhere real: the platforms' own endpoints are answered inside the test
# process, which is the only thing a test run may substitute.
#
# setdefault, not assignment, so a real deployment value passed in the
# environment still wins.
for _platform_variable, _placeholder in {
    "PLATFORM_FACEBOOK_APP_ID": "e2e-facebook-app",
    "PLATFORM_FACEBOOK_APP_SECRET": "e2e-facebook-app-secret",
    "PLATFORM_INSTAGRAM_APP_ID": "e2e-instagram-app",
    "PLATFORM_INSTAGRAM_APP_SECRET": "e2e-instagram-app-secret",
    "PLATFORM_TIKTOK_CLIENT_KEY": "e2e-tiktok-client",
    "PLATFORM_TIKTOK_CLIENT_SECRET": "e2e-tiktok-client-secret",
    "PLATFORM_GOOGLE_CLIENT_ID": "e2e-google-client",
    "PLATFORM_GOOGLE_CLIENT_SECRET": "e2e-google-client-secret",
    "PLATFORM_PINTEREST_APP_ID": "e2e-pinterest-app",
    "PLATFORM_PINTEREST_APP_SECRET": "e2e-pinterest-app-secret",
    "PLATFORM_LINKEDIN_PERSONAL_CLIENT_ID": "e2e-linkedin-personal",
    "PLATFORM_LINKEDIN_PERSONAL_CLIENT_SECRET": "e2e-linkedin-personal-secret",
    "PLATFORM_LINKEDIN_COMPANY_CLIENT_ID": "e2e-linkedin-company",
    "PLATFORM_LINKEDIN_COMPANY_CLIENT_SECRET": "e2e-linkedin-company-secret",
}.items():
    os.environ.setdefault(_platform_variable, _placeholder)

from .test import *  # noqa: E402, F403

# test.py runs the policy report-only. Enforce it here, so the browser really
# refuses what production refuses. Taken from CSP_POLICY rather than from
# SECURE_CSP_REPORT_ONLY so this reads as "e2e enforces the base policy",
# which is the intent, instead of a swap between two names.
SECURE_CSP = CSP_POLICY  # noqa: F405
SECURE_CSP_REPORT_ONLY = None

# live_server speaks plain HTTP. A redirect to https would end every test
# before it reached a page.
SECURE_SSL_REDIRECT = False

# live_server binds an arbitrary port on localhost.
ALLOWED_HOSTS = ["*"]
