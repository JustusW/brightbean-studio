"""Bring pictures out of the club's forum and into Brightbean. ONE DRAFT.

    python manage.py import_forum \
        --manifest - \
        --workspace <uuid> \
        --to <account-uuid> [--to <account-uuid> ...] \
        [--dry-run]

WHAT THIS IS FOR. Somebody posts a dozen photographs from a flying day
to the forum, and the club wants them on its website and its social
accounts. The shepherd, on doing that by hand: "I need a way to insert
pictures from the forum into BrightBean. Cause I sure as shit won't be
fucking copy pasting them."

IT MAKES ONE DRAFT, and that is the whole shape of it - his word.
Not one post per photograph: one post carrying all of them, left in
DRAFT, for a person to caption and send. Eight posts would be eight
things to write captions for and eight things to delete.

DRAFT IS THE SAFETY PROPERTY, and it is enforced by the model rather
than by this file being careful. PlatformPost.VALID_TRANSITIONS maps
draft to {publishing, scheduled, pending_review}, and the publisher only
ever collects rows that are already scheduled or publishing - so nothing
imported here can leave the building until a person moves it. That
matters because a caption-less photo dump reaching Instagram because an
importer ran would be an apology to the club, not a bug report.

    THE OPPOSITE CHOICE IS ALSO IN THIS TREE, deliberately.
    import_wordpress creates rows already PUBLISHED, because published
    is terminal and can never be handed to a provider either. Same
    mechanism, opposite end: that one is history that must never be
    re-posted, this one is new material that must not be posted YET.

THE IDEMPOTENCY KEY IS DISCOURSE'S UPLOAD SHA1, and it is the reason
this can be run twice without thinking. Discourse names every upload by
the SHA1 OF ITS CONTENT, so the same photograph posted twice in the
forum - by two members, in two topics - is one hash and therefore one
MediaAsset here. That is the dedup the shepherd asked for between the
Impressionen and the Instagram paths, and it comes free: one asset
attached to one post is one photograph however many channels carry it,
because the website's gallery is SELECT DISTINCT ON (media asset).

WHERE THE BYTES COME FROM. This runs INSIDE the web container, which
cannot see Discourse's uploads directory and has no business reaching
its database. So the extraction happens outside - see the Vogelwarte
action - and the files are staged into a directory under MEDIA_ROOT,
which IS mounted here. The manifest names them; this reads them and
hands them to Django's storage the same way import_wordpress does.

EXIF ORIENTATION IS NORMALISED ON THE WAY IN, and that is not tidiness.
The club's website serves photographs through nginx's image_filter,
which re-encodes and DROPS EXIF - so a phone photograph carrying an
orientation flag would look upright as an original and arrive sideways
once resized. Measured when that was built: none of the 73 pictures then
in the library carried a non-upright flag, but a phone upload is exactly
where one comes from. Fixing it at import means the stored file is
already upright and nothing downstream has to know.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

#: Written into Post.internal_notes so a second run recognises its own
#: work. A marker in a field nobody publishes beats a new column: this
#: is provenance as much as a key, and in a year somebody will want to
#: know why a post appeared carrying eight photographs nobody uploaded
#: through the composer.
MARKER = "forum-import"

#: How a MediaAsset records where it came from. `source_url` is what
#: import_wordpress keys on too, so the two importers cannot collide
#: even if the same picture somehow reached both.
SOURCE = "discourse"


def upload_key(sha1: str) -> str:
    """The dedup key for one Discourse upload.

    A URL-shaped string rather than a bare hash, because source_url is
    what it goes in and a bare hash there reads like a mistake. The hash
    is the part that matters: Discourse derives it from the CONTENT, so
    this is content-addressed and the same photograph is the same key
    wherever it was posted.
    """
    return f"discourse://upload/{sha1}"


class Command(BaseCommand):
    help = "Import a forum topic's pictures as ONE draft post."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True,
                            help="the extraction, as JSON. '-' reads stdin, "
                                 "which is how the control plane feeds it - "
                                 "no mount, nothing left on disk")
        parser.add_argument("--workspace", required=True,
                            help="workspace UUID the post belongs to")
        parser.add_argument("--to", action="append", default=[],
                            help="social account UUID to attach a DRAFT "
                                 "platform post to. Repeatable. None means "
                                 "the post is made with no targets at all, "
                                 "which is a perfectly good draft.")
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

        accounts = []
        for ident in options["to"]:
            try:
                account = SocialAccount.objects.get(pk=ident)
            except (SocialAccount.DoesNotExist, ValueError, TypeError) as exc:
                raise CommandError(f"no such social account: {ident}") from exc
            # THE ACCOUNT MUST BELONG TO THE WORKSPACE. This workspace
            # holds two brands, and attaching the club's photographs to
            # SkyMasters' Instagram would be invisible here and obvious
            # on somebody else's feed.
            if account.workspace_id != workspace.id:
                raise CommandError(
                    f"the account {account} belongs to workspace "
                    f"{account.workspace_id}, not to {workspace.id}. "
                    "Refusing: this workspace holds more than one brand.")
            accounts.append(account)

        if options["manifest"] == "-":
            manifest = json.load(sys.stdin)
        else:
            with open(options["manifest"], encoding="utf-8") as fh:
                manifest = json.load(fh)

        topic = manifest.get("topic") or {}
        uploads = manifest.get("uploads") or []
        if not uploads:
            raise CommandError(
                "the manifest carries no uploads. Nothing to import - and "
                "an empty draft is worse than none.")

        marker = f"{MARKER}:topic-{topic.get('id')}"

        self.stdout.write(
            f"workspace {workspace}\n"
            f"topic     {topic.get('id')} :: {topic.get('title', '')}\n"
            f"pictures  {len(uploads)}\n"
            f"targets   "
            + (", ".join(f"{a.platform}/{a.account_name}" for a in accounts)
               or "(none - a draft with no channels chosen)")
            + "\n")
        if dry:
            self.stdout.write(self.style.WARNING(
                "DRY RUN - nothing is written.\n"))

        existing = Post.objects.filter(
            workspace=workspace, internal_notes__contains=marker).first()
        if existing is not None:
            # ADOPTED RATHER THAN DUPLICATED. Re-running after somebody
            # adds two more photographs to the topic should add those
            # two, not build a second post carrying all of them.
            self.stdout.write(
                f"  = post already imported for this topic ({existing.id})")
            self.stdout.write("    new pictures will be added to it")

        made = adopted = 0
        assets = []
        for entry in uploads:
            asset, created = self._asset(entry, workspace, dry)
            if asset is not None or dry:
                assets.append((entry, asset))
            made += 1 if created else 0
            adopted += 0 if created else 1

        if dry:
            self.stdout.write(self.style.SUCCESS(
                f"\nwould add {made} picture(s), {adopted} already known."))
            return

        post = self._post(existing, manifest, workspace, marker, assets)
        self._targets(post, accounts)

        self.stdout.write(self.style.SUCCESS(
            f"\n{made} picture(s) imported, {adopted} already there.\n"
            f"ONE DRAFT: {post.id}\n"
            f"Open it in the composer, write the caption, choose where it "
            f"goes. Nothing here can publish itself."))

    # ------------------------------------------------------------------

    def _asset(self, entry, workspace, dry):
        """One MediaAsset for one upload. (asset, was_created)."""
        sha1 = (entry.get("sha1") or "").strip().lower()
        if not sha1:
            self.stdout.write("    ! an upload has no sha1 - skipped")
            return None, False

        key = upload_key(sha1)
        found = MediaAsset.objects.filter(
            workspace=workspace, source_url=key).first()
        if found is not None:
            self.stdout.write(f"  = {entry.get('filename')}  (already here)")
            return found, False

        name = entry.get("filename") or f"{sha1}.jpg"
        self.stdout.write(f"  + {name}  {entry.get('width')}x"
                          f"{entry.get('height')}")
        if dry:
            return None, True

        # STAGED UNDER MEDIA_ROOT, because that is the one directory this
        # container and the host agree on - /app/media here is
        # /srv/brightbean/media there. The inbox the installer makes is
        # NOT mounted in, which is why the files come this way round.
        staged = Path(settings.MEDIA_ROOT) / entry.get("staged", "")
        try:
            staged_resolved = staged.resolve()
            root = Path(settings.MEDIA_ROOT).resolve()
        except OSError as exc:
            raise CommandError(f"cannot resolve {staged}: {exc}") from exc
        # The manifest is generated by us, but it still names a path that
        # is opened here - so it is confined to MEDIA_ROOT rather than
        # trusted. A staging path escaping upwards would be a read of
        # anything this process can reach.
        if not staged_resolved.is_relative_to(root):
            raise CommandError(
                f"refusing a staged path outside MEDIA_ROOT: {staged}")
        if not staged_resolved.is_file():
            self.stdout.write(f"    ! {staged} is not there - skipped")
            return None, False

        body = staged_resolved.read_bytes()
        body, width, height = self._upright(body, entry)

        asset = MediaAsset(
            organization=workspace.organization,
            workspace=workspace,
            filename=name,
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type=entry.get("mime") or "image/jpeg",
            file_size=len(body),
            width=width or entry.get("width") or 0,
            height=height or entry.get("height") or 0,
            # NO INVENTED ALT TEXT. Nobody here has seen the photograph,
            # and a made-up description of one is worse than none - it
            # reads as a description to anybody relying on it. The forum
            # gives us no alt text, so this stays empty until a person
            # writes one.
            alt_text="",
            title=name[:255],
            source=SOURCE,
            source_url=upload_key(sha1),
            # WHO TOOK IT, as far as the forum knows. Not decoration:
            # these are members' photographs, and the club publishing one
            # should be able to say whose it is.
            attribution=(entry.get("username") or "")[:255],
        )
        asset.file.save(name, ContentFile(body), save=False)
        asset.save()
        return asset, True

    def _upright(self, body, entry):
        """Bytes with EXIF orientation applied. (body, width, height).

        ONLY RE-ENCODES WHEN IT HAS TO. A photograph already upright is
        passed through BYTE FOR BYTE - re-encoding every import would
        cost a generation of JPEG quality on every picture to fix the
        few that need it.

        Never raises: a picture Pillow cannot read is still a picture
        the club uploaded, and it goes in as it came rather than being
        dropped for failing a check that is a nicety.
        """
        try:
            import io

            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(body)) as im:
                orientation = (im.getexif() or {}).get(274)
                if orientation in (None, 0, 1):
                    return body, im.width, im.height

                self.stdout.write(
                    f"    EXIF orientation {orientation} - rewriting it "
                    f"upright, because the website's resizer drops EXIF "
                    f"and this would arrive sideways")
                fixed = ImageOps.exif_transpose(im)
                out = io.BytesIO()
                # 92: high enough that one re-encode of a photograph is
                # not visible, and this happens once per picture ever.
                fixed.save(out, format=im.format or "JPEG", quality=92)
                return out.getvalue(), fixed.width, fixed.height
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.stdout.write(f"    (could not check orientation: {exc})")
        return body, entry.get("width") or 0, entry.get("height") or 0

    @transaction.atomic
    def _post(self, existing, manifest, workspace, marker, assets):
        """THE one draft, made or adopted, with the pictures attached."""
        topic = manifest.get("topic") or {}
        title = (topic.get("title") or "Bilder aus dem Forum")[:255]

        if existing is None:
            post = Post.objects.create(
                workspace=workspace,
                title=title,
                # NO CAPTION. The forum post's text is a member talking
                # to other members - "hier sind auch noch ein paar
                # Bilder" - and publishing that as the club's caption on
                # Instagram would be putting somebody's aside in the
                # club's mouth. The draft is where a caption gets
                # written; this leaves it blank on purpose.
                caption="",
                tags=["Forum"],
                internal_notes=(
                    f"{marker}\n"
                    f"Aus dem Forum: {topic.get('url') or ''}\n"
                    f"Thema {topic.get('id')} :: {title}\n"
                    f"Kategorie: {topic.get('category') or 'unbekannt'}\n"
                    f"Bilder von: "
                    + ", ".join(sorted({
                        e.get("username") or "?" for e, _ in assets})) + "\n"
                ),
            )
            self.stdout.write(f"\n  draft created {post.id}")
        else:
            post = existing

        # THE ORDER THEY WERE POSTED IN, and appended rather than
        # renumbered from zero - so a second run that brings two new
        # photographs puts them after the six already there instead of
        # colliding on position 0.
        position = (post.media_attachments.count()
                    if existing is not None else 0)
        for entry, asset in assets:
            if asset is None:
                continue
            if PostMedia.objects.filter(post=post, media_asset=asset).exists():
                continue
            PostMedia.objects.create(
                post=post, media_asset=asset, position=position, alt_text="")
            position += 1
        return post

    def _targets(self, post, accounts):
        """A DRAFT platform post per channel. Idempotent."""
        for account in accounts:
            if PlatformPost.objects.filter(
                    post=post, social_account=account).exists():
                self.stdout.write(
                    f"  = {account.platform}/{account.account_name} "
                    f"already attached")
                continue
            PlatformPost.objects.create(
                post=post,
                social_account=account,
                # DRAFT, AND THE MODEL IS WHAT ENFORCES IT. draft
                # transitions only to publishing, scheduled or
                # pending_review, and the publisher collects neither
                # draft nor anything but scheduled and publishing - so
                # this cannot go out until a person moves it.
                status=PlatformPost.Status.DRAFT,
                platform_post_id="",
            )
            self.stdout.write(
                f"  + {account.platform}/{account.account_name}  (draft)")
