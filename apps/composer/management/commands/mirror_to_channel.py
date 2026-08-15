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

IT MAKES A REAL COPY OF THE POST, AND THE FIRST VERSION DID NOT.

That version created one more PlatformPost against the SAME Post, and
this docstring argued for it: a Post already carries the title, caption
and media, so pointing a second channel at it duplicates nothing. It is
a tidy argument and it is wrong, because it makes the two channels ONE
THING wearing two names. The shepherd found out how wrong by curating
the picture wall - removing ten photographs he did not want on
Impressionen - and watching them vanish from the club's front page and
its Instagram archive at the same time, because all three were editing
one row:

    "I told you to copy the posts... And instead you fucking linked
     them all... Now Aktuelles has lost all images that I wanted gone
     from Impressionen"

He said COPY. This now copies: a new Post with its own PostMedia rows,
pointing at the same MediaAssets in the library. Editing one channel's
post cannot reach another's, which is the entire point of giving a
channel its own posts.

THE ASSETS ARE STILL SHARED, and that is correct rather than a
half-measure: a MediaAsset is the photograph itself, one file on disk.
Copying files per channel would multiply the media library by the
number of channels and mean a re-crop had to be done three times.
Removing a picture from a post detaches it; it does not delete it.

THE PICTURE WALL STILL DOES NOT DOUBLE UP, for the same reason as
before: its query is SELECT DISTINCT ON (ma.id), so it dedupes on the
ASSET, and two copies pointing at one photograph show it once.

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

import uuid

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.credentials.models import PlatformCredential
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.impressionen import channel_id

#: WHAT A COPY REMEMBERS IT IS A COPY OF, written into internal_notes.
#:
#: The old version keyed idempotency on (post, target channel), which
#: worked only because both channels shared one Post. Now that each gets
#: its own, the source post has NO platform post on the target and that
#: test would answer "not done yet" every single time - so a second run
#: would build a parallel set of copies, and a third another.
#:
#: internal_notes rather than a column: this is provenance as much as a
#: key, and in a year somebody will want to know why two posts carry the
#: same caption.
MIRROR_MARKER = "mirror-of:"


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
            # ALREADY COPIED? Asked of the MARKER, not of the source post's
            # platform posts - the copy is a different row, so the source
            # has nothing on the target to find. See MIRROR_MARKER.
            if target is not None and Post.objects.filter(
                    workspace=workspace,
                    platform_posts__social_account=target,
                    internal_notes__contains=f"{MIRROR_MARKER}{original.post_id}",
            ).exists():
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
            # THE NAME, AND IT MUST BE THE NAME. Not a credential and not
            # a secret: this channel reaches no network. It is stored
            # because several code paths test `if
            # account.oauth_access_token:` to decide whether an account is
            # worth acting on, and an empty string there reads as "never
            # connected" rather than "needs nothing".
            #
            # THIS SAID PLACEHOLDER_TOKEN AND IT EMPTIED THE FRONT PAGE
            # OVERNIGHT. ImpressionenProvider.get_profile reads the stored
            # token AS THE CHANNEL'S NAME, and falls back to DEFAULT_NAME
            # for a blank or placeholder one - so the first health check
            # to validate this account renamed "Aktuelles" to
            # "Impressionen", rewrote its handle to match, and the
            # website's feed - which pins by name - matched nothing.
            # Measured the next morning: {"items":[]} on a site that had
            # been correct at midnight, with nothing in any log to say
            # why, because nothing had gone wrong. A validation had
            # succeeded.
            #
            # The name IS the token for this provider. Storing anything
            # else is storing a rename with a delay on it.
            oauth_access_token=name[:255],
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        # The same thing the connect view does, so a channel made here
        # behaves like one made through the UI.
        from apps.calendar.services import create_default_queue_and_slots
        create_default_queue_and_slots(account)
        return account, True

    @transaction.atomic
    def _copy(self, original, target):
        """A COPY of the post, on the target channel. Not a second pointer.

        The post is duplicated with its own PostMedia rows; the
        MediaAssets themselves are shared, because those are the
        photographs and there is one of each. See the module docstring
        for what linking instead of copying cost.
        """
        source = original.post

        copy = Post.objects.get(pk=source.pk)
        copy.pk = uuid.uuid4()
        copy._state.adding = True
        # WHERE IT CAME FROM - the idempotency key and the provenance.
        #
        # AND THE IMPORT MARKER IS DEFUSED IN THE SAME BREATH.
        # import_wordpress finds its own work with
        # `internal_notes__contains=marker` and --prune DELETES any post
        # carrying the marker it no longer claims. Three posts sharing one
        # marker would make it pick one arbitrarily and leave the copies
        # deletable by a later import. "wordpress-import-copy:" does not
        # start with "wordpress-import:", so prune's startswith test
        # passes over it.
        notes = (source.internal_notes or "").replace(
            "wordpress-import:", "wordpress-import-copy:")
        copy.internal_notes = (
            f"{notes}\n{MIRROR_MARKER}{source.pk}\n"
            f"Eigener Beitrag des Kanals {target.account_name}.\n")
        copy.save()
        # created_at is auto_now_add, and the website's feed uses it as
        # the tiebreak when published_at is null - so a copy that kept
        # today's date would sort to the top as though it were news.
        Post.objects.filter(pk=copy.pk).update(created_at=source.created_at)

        for attachment in source.media_attachments.order_by("position"):
            PostMedia.objects.create(
                post=copy,
                media_asset=attachment.media_asset,
                position=attachment.position,
                alt_text=attachment.alt_text,
            )

        PlatformPost.objects.create(
            post=copy,
            social_account=target,
            status=PlatformPost.Status.PUBLISHED,
            published_at=original.published_at,
            # EMPTY, deliberately. There is no post on any platform here
            # - this channel has no API - and carrying Instagram's id
            # across would be a lie something downstream would eventually
            # try to fetch.
            platform_post_id="",
        )
