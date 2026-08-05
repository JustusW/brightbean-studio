from django.db import migrations, models

PLATFORM_CHOICES = [
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("instagram_login", "Instagram (Direct)"),
    ("linkedin_personal", "LinkedIn (Personal Profile)"),
    ("linkedin_company", "LinkedIn (Company Page)"),
    ("tiktok", "TikTok"),
    ("youtube", "YouTube"),
    ("pinterest", "Pinterest"),
    ("threads", "Threads"),
    ("bluesky", "Bluesky"),
    ("google_business", "Google Business Profile"),
    ("mastodon", "Mastodon"),
    ("devto", "DEV.to"),
    ("wordpress", "WordPress"),
]


class Migration(migrations.Migration):
    # No visibility or analytics rows are seeded for the new platform, which
    # matches how devto was added in 0012: a platform with no
    # PlatformVisibility row defaults to VISIBLE, and
    # AnalyticsPlatformConfig.enabled_platforms() only honours rows that
    # exist. WordPress has no analytics endpoint to sync anyway.
    dependencies = [
        ("social_accounts", "0012_add_devto_platform"),
    ]

    operations = [
        migrations.AlterField(
            model_name="socialaccount",
            name="platform",
            field=models.CharField(choices=PLATFORM_CHOICES, max_length=30),
        ),
        migrations.AlterField(
            model_name="platformvisibility",
            name="platform",
            field=models.CharField(choices=PLATFORM_CHOICES, max_length=30, unique=True),
        ),
        migrations.AlterField(
            model_name="analyticsplatformconfig",
            name="platform",
            field=models.CharField(choices=PLATFORM_CHOICES, max_length=30, unique=True),
        ),
    ]
