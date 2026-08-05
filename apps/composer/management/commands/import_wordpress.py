"""Import the club's old WordPress site into this workspace, as history.

    python manage.py import_wordpress \
        --manifest research/wp_items.json \
        --media-dir research/wp_media \
        --workspace <uuid> \
        --account <uuid> \
        [--dry-run]

WHAT THIS IS FOR. The Verein für Modellflug Stutensee ran a WordPress
site from 2016, and everything it published lives inside the markup of
two pages rather than as articles — /wp-json answers ZERO posts. It was
scraped into a manifest of items, each one a heading, its prose and the
photographs that appeared with it. This turns those into real rows.

IT CREATES THEM ALREADY PUBLISHED, AND THAT IS THE SAFETY PROPERTY.

The instruction was that these must NEVER be posted to Instagram. That
is not a flag anybody has to remember, and it is not a new kind of
account: PlatformPost.published is a TERMINAL state — look at
VALID_TRANSITIONS, where "published" maps to the empty set — and the
publisher only ever picks up rows that are scheduled or publishing. A
row that arrives already published is therefore never handed to a
provider, by construction.

The same fact is what makes them appear on the club's website:
brightbean-surface matches published platform posts on the configured
account, deliberately, because a composer_post has no status of its own.
One mechanism, both jobs.

IT IS IDEMPOTENT. Every post carries a marker in internal_notes and
every asset its source_url, so a second run adopts what is already there
instead of building a parallel copy of the club's history. Anything that
runs against a live database has to be safe to run twice, because the
first run is the one that gets interrupted.

NOTHING IS INVENTED. An item the scrape could not date is imported with
no published_at rather than with today's date — a club history that all
happened this afternoon is worse than one with a gap. The feed sorts
NULLS LAST, so it lands at the end rather than pretending to be news.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.request
from datetime import datetime, timezone as utc

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

#: Written into internal_notes so a second run can recognise its own
#: work. A marker in a field nobody publishes is better than a new
#: column: this is a one-off migration, and a schema change for it would
#: outlive the reason for it.
MARKER = "wordpress-import"

AGENT = "Mozilla/5.0 (compatible; VFM-site-migration/1.0)"


class Command(BaseCommand):
    help = "Import the club's old WordPress content as published history."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True,
                            help="research/wp_items.json from the scrape")
        parser.add_argument("--media-dir", required=True,
                            help="where the scraped images were downloaded")
        parser.add_argument("--workspace", required=True,
                            help="workspace UUID the posts belong to")
        parser.add_argument("--account", required=True,
                            help="social account UUID to attach them to")
        parser.add_argument("--dry-run", action="store_true",
                            help="say what would happen and write nothing")
        parser.add_argument("--prune", action="store_true",
                            help="delete imported posts the manifest no "
                                 "longer lists")

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        dry = options["dry_run"]

        try:
            workspace = Workspace.objects.get(pk=options["workspace"])
        except (Workspace.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(f"no such workspace: {options['workspace']}") from exc

        try:
            account = SocialAccount.objects.get(pk=options["account"])
        except (SocialAccount.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(f"no such social account: {options['account']}") from exc

        # THE ACCOUNT MUST BELONG TO THE WORKSPACE. Attaching this club's
        # history to another brand's account would be invisible here and
        # obvious on the wrong website — and this workspace holds two
        # brands.
        if account.workspace_id != workspace.id:
            raise CommandError(
                f"the account {account} belongs to workspace "
                f"{account.workspace_id}, not to {workspace.id}. Refusing: "
                "this would file the club's history under another brand.")

        # "-" MEANS STDIN, and that is how the control plane feeds this.
        #
        # The web container mounts only ./media, so a manifest written to
        # the host's inbox is not visible in here — and putting it under
        # media would publish the club's import manifest at
        # files.wingert.dev, which is a silly place for it. Piping it in
        # needs no mount, no new volume and no file left behind.
        if options["manifest"] == "-":
            manifest = json.load(sys.stdin)
        else:
            with open(options["manifest"], encoding="utf-8") as fh:
                manifest = json.load(fh)

        self.stdout.write(
            f"workspace {workspace}\n"
            f"account   {account.platform} / {account.account_name}\n"
            f"posts     {len(manifest.get('posts', []))}\n"
            f"media     {sum(len(i['images']) for i in manifest.get('media_only', []))}"
            f" loose photographs\n")
        if dry:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing is written.\n"))

        made = adopted = 0
        for item in manifest.get("posts", []):
            created = self._one_post(item, workspace, account, options, dry)
            made += 1 if created else 0
            adopted += 0 if created else 1

        # The loose photographs — was-hier-fliegt is eleven aircraft with
        # no prose at all. They are media, not posts, so they go into the
        # library and nowhere near the feed.
        loose = 0
        for item in manifest.get("media_only", []):
            for image in item["images"]:
                if self._one_asset(image, workspace, options, dry):
                    loose += 1

        # ANYTHING THIS IMPORT ONCE CREATED AND NO LONGER CLAIMS.
        #
        # The judgement about what IS a post lives in the manifest, and it
        # changes: "Anfahrt über L 560" sits on the news page but is
        # directions, and its picture — the club's hand-drawn
        # Anfahrtskizze — had gone into the feed and the gallery among the
        # aeroplanes. Reclassifying it upstream fixes the next import and
        # does nothing at all about the row already in the database.
        #
        # So the marker works in both directions: a post carrying this
        # import's mark whose marker is no longer in the manifest is one
        # this import made and has since disowned. Only ever those — a
        # post somebody wrote in Brightbean carries no marker and cannot
        # be touched by this.
        pruned = 0
        if options["prune"]:
            keep = {self._marker(i) for i in manifest.get("posts", [])}
            for post in Post.objects.filter(
                    workspace=workspace, internal_notes__contains=MARKER):
                mine = next((line for line in post.internal_notes.splitlines()
                             if line.startswith(f"{MARKER}:")), "")
                if mine and mine not in keep:
                    self.stdout.write(
                        f"  - {post.title[:60]}  (no longer in the manifest)")
                    if not dry:
                        post.delete()
                    pruned += 1

        # AND THE PICTURES THE MANIFEST HAS STOPPED CLAIMING.
        #
        # Pruning posts alone leaves a real gap: the club's mascot, a
        # visiting club's banner and a scanned invitation were all
        # dropped from posts that STILL EXIST, so those posts are adopted
        # untouched and the junk stays attached. Deleting the asset is
        # what actually removes it — PostMedia cascades from it, so the
        # picture leaves the post and the library together.
        #
        # Scoped to source="wordpress": nothing uploaded by a person in
        # Brightbean carries that, and nothing here can touch it.
        wanted = {
            image.get("src", "")
            for group in ("posts", "media_only", "page_content")
            for item in manifest.get(group, [])
            for image in item.get("images", [])
        }
        for asset in MediaAsset.objects.filter(workspace=workspace,
                                               source="wordpress"):
            if asset.source_url and asset.source_url not in wanted:
                self.stdout.write(
                    f"  - {asset.filename}  (no longer in the manifest)")
                if not dry:
                    asset.delete()
                pruned += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n{made} post(s) created, {adopted} already there, "
            f"{pruned} removed, {loose} loose photograph(s) added."))

    # ------------------------------------------------------------------

    def _marker(self, item) -> str:
        """A stable name for this item, so a rerun recognises it."""
        return f"{MARKER}:{item['slug']}#{item['heading'][:80]}"

    def _when(self, item):
        """The item's date as a datetime, or None. Never invented."""
        raw = item.get("date") or ""
        try:
            if len(raw) == 7:            # YYYY-MM
                return datetime.strptime(raw, "%Y-%m").replace(
                    day=1, hour=12, tzinfo=utc.utc)
            if len(raw) == 4:            # YYYY
                return datetime.strptime(raw, "%Y").replace(
                    month=1, day=1, hour=12, tzinfo=utc.utc)
        except ValueError:
            pass
        return None

    def _one_asset(self, image, workspace, options, dry):
        """One MediaAsset, or the existing one. Returns True if created."""
        source_url = image.get("src", "")
        if not source_url:
            return False

        # source_url is the identity: the same photograph fetched twice
        # is the same photograph.
        if MediaAsset.objects.filter(workspace=workspace,
                                     source_url=source_url).exists():
            return False

        name = image.get("file") or os.path.basename(source_url.split("?")[0])
        if not name:
            return False
        path = os.path.join(options["media_dir"], name)

        if dry:
            self.stdout.write(f"    + asset {name}")
            return True

        if os.path.exists(path):
            with open(path, "rb") as fh:
                body = fh.read()
        else:
            # The scrape saved these, but a manifest can outlive its
            # download directory — fetch rather than fail.
            request = urllib.request.Request(
                source_url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()

        mime = mimetypes.guess_type(name)[0] or "image/jpeg"
        asset = MediaAsset(
            organization=workspace.organization,
            workspace=workspace,
            filename=name,
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type=mime,
            file_size=len(body),
            width=image.get("width") or 0,
            height=image.get("height") or 0,
            # The club wrote NO alt text on any of the 232 items in that
            # library — measured. The caption is the best description
            # there is, and an empty alt is honest where there is none:
            # inventing a description of a photograph nobody here has
            # seen would be worse than silence.
            alt_text=(image.get("alt") or image.get("caption") or "").strip(),
            title=(image.get("title") or "").strip()[:255],
            source="wordpress",
            source_url=source_url,
        )
        asset.file.save(name, ContentFile(body), save=False)
        asset.save()
        self.stdout.write(f"    + asset {name} ({len(body):,} bytes)")
        return True

    @transaction.atomic
    def _one_post(self, item, workspace, account, options, dry):
        """One Post with its media and its published PlatformPost."""
        marker = self._marker(item)
        existing = Post.objects.filter(workspace=workspace,
                                       internal_notes__contains=marker).first()
        heading = item["heading"] or "(ohne Titel)"

        if existing:
            self.stdout.write(f"  = {heading[:60]}  (already imported)")
            return False

        when = self._when(item)
        stamp = item.get("date") or "undated"
        self.stdout.write(
            f"  + {stamp:8s} {heading[:52]:52s} "
            f"{len(item['images'])} image(s)")

        if dry:
            for image in item["images"]:
                self._one_asset(image, workspace, options, dry)
            return True

        post = Post.objects.create(
            workspace=workspace,
            title=heading[:255],
            caption="\n\n".join(item["text"]).strip(),
            tags=["Archiv", "Website"],
            published_at=when,
            # WHERE IT CAME FROM, in a field that is never published. It
            # is both the idempotency key and the provenance: in a year
            # somebody will want to know why a 2018 post appeared in the
            # database in 2026.
            internal_notes=(
                f"{marker}\n"
                f"Übernommen von www.modellflug-stutensee.de, Seite "
                f"/{item['slug']}.\n"
                f"Datierung: {stamp} ({item.get('date_from') or 'unbekannt'})."
            ),
        )

        for position, image in enumerate(item["images"]):
            self._one_asset(image, workspace, options, dry)
            asset = MediaAsset.objects.filter(
                workspace=workspace, source_url=image.get("src", "")).first()
            if asset:
                PostMedia.objects.create(
                    post=post,
                    media_asset=asset,
                    position=position,
                    alt_text=(image.get("alt") or image.get("caption")
                              or "").strip(),
                )

        # ALREADY PUBLISHED. This is the whole guard: published is a
        # terminal state in PlatformPost.VALID_TRANSITIONS and the
        # publisher only collects scheduled or publishing rows, so this
        # can never be sent to Instagram. platform_post_id stays empty
        # because there is no post on the platform — this was published
        # on the club's own website years ago, and pretending we have an
        # Instagram id for it would be a lie that something downstream
        # would eventually try to fetch.
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            published_at=when,
            platform_post_id="",
        )
        return True
