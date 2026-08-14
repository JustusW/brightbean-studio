"""Copy published posts onto a named channel that publishes nowhere.

    python manage.py mirror_to_channel \
        --workspace <uuid> \
        --from-account <uuid> \
        --to "Aktuelles" \
        [--dry-run]

WHAT THIS IS FOR. The club's website took its front page straight from
Instagram: brightbean-surface matched published platform posts on the
Instagram account, so "Aktuelles" was whatever had last gone to
Instagram, with the caption that had been written for Instagram.

Moving the front page onto its own channel fixes that - the club can
post to its website without posting to Instagram, and write for the web
- but it would also empty the front page on the day it is switched,
because none of the existing history is on the new channel. This puts
it there.

IT COPIES THE PLATFORM POST, NOT THE POST. A Post already carries its
title, caption, tags and media; what says "this appeared on that
channel" is a PlatformPost. So each post gains one more, on the new
channel, and keeps its Instagram one. Nothing is duplicated: the same
Post and the same MediaAssets are pointed at from both.

WHICH IS ALSO WHY THE PICTURE WALL DOES NOT DOUBLE UP. Its query is
SELECT DISTINCT ON (ma.id) - it dedupes on the media asset - so a
photograph reachable through two channels appears once.

CREATED ALREADY PUBLISHED, and that is the safety property rather than
a shortcut. PlatformPost.published is a TERMINAL state - look at
VALID_TRANSITIONS, where "published" maps to the empty set - and the
publisher only ever collects rows that are scheduled or publishing. A
row that arrives published is therefore never handed to a provider. It
cannot be posted anywhere, and the target channel has no API behind it
in any case.

IT IS IDEMPOTENT. The key is (post, target channel): a post that
already has a platform post on the target is skipped. Anything that
runs against a live database has to be safe to run twice, because the
first run is the one that gets interrupted.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.composer.models import PlatformPost
from apps.credentials.models import PlatformCredential
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.impressionen import PLACEHOLDER_TOKEN, channel_id


class Command(BaseCommand):
    help = ("Copy published posts from one account onto a named channel "
            "that publishes nowhere.")

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True,
                            help="workspace UUID the posts belong to")
        parser.add_argument("--from-account", required=True,
                            help="social account UUID to copy FROM")
        parser.add_argument("--to", required=True,
                            help="name of the channel to copy TO; it is "
                                 "created if it does not exist")
        parser.add_argument("--platform",
                            default=PlatformCredential.Platform.IMPRESSIONEN,
                            help="platform the target channel sits on")
        parser.add_argument("--dry-run", action="store_true",
                            help="say what would happen and write nothing")

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        dry = options["dry_run"]

        try:
            workspace = Workspace.objects.get(pk=options["workspace"])
        except (Workspace.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(
                f"no such workspace: {options['workspace']}") from exc

        try:
            source = SocialAccount.objects.get(pk=options["from_account"])
        except (SocialAccount.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(
                f"no such social account: {options['from_account']}") from exc

        # THE SOURCE MUST BELONG TO THE WORKSPACE. This workspace holds
        # two brands, and copying one brand's history onto the other's
        # website channel would be invisible here and obvious on the
        # wrong front page.
        if source.workspace_id != workspace.id:
            raise CommandError(
                f"the account {source} belongs to workspace "
                f"{source.workspace_id}, not to {workspace.id}. Refusing: "
                "this workspace holds more than one brand.")

        name = (options["to"] or "").strip()
        if not name:
            raise CommandError("--to needs a channel name")

        target, made = self._channel(workspace, options["platform"], name, dry)

        self.stdout.write(
            f"workspace {workspace}\n"
            f"from      {source.platform} / {source.account_name}\n"
            f"to        {options['platform']} / {name}"
            f"{' (created)' if made else ''}\n")
        if dry:
            self.stdout.write(self.style.WARNING(
                "DRY RUN - nothing is written.\n"))

        # ONLY WHAT WAS ACTUALLY PUBLISHED. A draft or a failed post on
        # the source never appeared on the website, so copying it would
        # be putting something on the front page that was never there.
        published = (PlatformPost.objects
                     .filter(social_account=source,
                             status=PlatformPost.Status.PUBLISHED)
                     .select_related("post")
                     .order_by("published_at"))

        copied = skipped = 0
        for original in published:
            if original.post.workspace_id != workspace.id:
                continue
            if target is not None and PlatformPost.objects.filter(
                    post=original.post, social_account=target).exists():
                skipped += 1
                continue

            when = original.published_at
            stamp = when.date().isoformat() if when else "undated"
            title = (original.post.title
                     or original.post.caption or "")[:52].replace("\n", " ")
            self.stdout.write(f"  + {stamp:10s} {title}")

            if not dry:
                self._copy(original, target)
            copied += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n{copied} post(s) copied onto {name}, "
            f"{skipped} already there."))

    # ------------------------------------------------------------------

    def _channel(self, workspace, platform, name, dry):
        """The target channel, made if it is not there. (account, created)."""
        ident = channel_id(name)
        existing = SocialAccount.objects.filter(
            workspace=workspace, platform=platform,
            account_platform_id=ident).first()
        if existing is not None:
            return existing, False

        if dry:
            self.stdout.write(
                f"  would create channel {platform} / {name} ({ident})")
            return None, True

        account = SocialAccount.objects.create(
            workspace=workspace,
            platform=platform,
            account_platform_id=ident,
            account_name=name[:255],
            account_handle=ident,
            avatar_url="",
            follower_count=0,
            # Not a credential and not a secret: this channel reaches no
            # network. It is stored because several code paths test
            # `if account.oauth_access_token:` to decide whether an
            # account is worth acting on, and an empty string there reads
            # as "never connected" rather than "needs nothing".
            oauth_access_token=PLACEHOLDER_TOKEN,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        # The same thing the connect view does, so a channel made here
        # behaves like one made through the UI.
        from apps.calendar.services import create_default_queue_and_slots
        create_default_queue_and_slots(account)
        return account, True

    @transaction.atomic
    def _copy(self, original, target):
        """One more PlatformPost, on the target channel."""
        PlatformPost.objects.create(
            post=original.post,
            social_account=target,
            status=PlatformPost.Status.PUBLISHED,
            published_at=original.published_at,
            # EMPTY, deliberately. There is no post on any platform here
            # - this channel has no API - and carrying Instagram's id
            # across would be a lie something downstream would eventually
            # try to fetch.
            platform_post_id="",
        )
