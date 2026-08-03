"""Photograph a platform's OWN API reference, so a stub can be built from it.

WHY THIS EXISTS. The endpoint tables in test_compose_and_publish.py were
discovered by driving the product with an empty table and reading what went
unanswered. That grounds them in OUR implementation: if a provider calls the
wrong endpoint or sends a malformed body, the stub answers it happily and the
test goes green. Such a table can only show the product is self-consistent.
It is a mirror, and what is wanted is an oracle.

The standing rule is official aids first, then community aids, then the
official API documentation. This reaches the third, which is the one that is
always available: it opens the platform's own reference page and keeps a
picture of it.

WHY A BROWSER. These pages are single-page applications. curl returns three
quarters of a megabyte of script tags and no schema at all; what a person
reads is what the browser renders.

WHY PICTURES rather than text. A screenshot of the response table is the
thing a person would read, at a size they could read it, and it can be looked
at directly. Pass a section anchor to get that section at legible size
instead of the whole page shrunk to nothing.

    uv run --python 3.13 --no-project --with-requirements requirements.txt \
        python tests/e2e/read_the_docs.py tiktok-direct-post Response

Output goes to .api-reference/, NOT to .e2e-screens/ - that directory is
emptied at the start of every browser run, which would delete these the
moment the suite next runs.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

WHERE = Path(__file__).resolve().parents[2] / ".api-reference"

PAGES = {
    # TikTok: what our provider calls is /v2/post/publish/video/init/ with
    # post_info + source_info, so Direct Post is the reference that governs
    # it; the media transfer guide covers the upload that follows.
    "tiktok-direct-post": "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
    "tiktok-media-transfer": "https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide",
    # YouTube: videos.insert is a resumable upload, so the protocol guide is
    # what describes the two-step exchange.
    "youtube-resumable-upload": "https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol",
    "youtube-uploading-a-video": "https://developers.google.com/youtube/v3/guides/uploading_a_video",
}


def extract_the_text(paper):
    """Lift the printed PDF's text out, which is what a schema actually is.

    NAMED FOR WHAT IT DOES. This was called `rasterise` and its docstring
    promised "a PNG per page at 150 dpi" - it renders nothing at all. The
    pictures were dropped once the printed text turned out to carry the same
    sentences at a fraction of what an image costs to read, and the name was
    left behind describing the version that no longer exists.
    """
    import pypdfium2

    document = pypdfium2.PdfDocument(paper)

    # THE TEXT FIRST, because it is what a schema actually is. A rendered
    # page costs a great deal to look at and carries the same sentences; the
    # printed PDF has them as text, exactly as the platform wrote them.
    # Pictures are for the layout questions text cannot answer.
    words = []
    for number, page in enumerate(document, start=1):
        words.append(f"\n----- printed page {number} -----\n")
        words.append(page.get_textpage().get_text_range())
    written = paper.with_suffix(".txt")
    written.write_text("".join(words), encoding="utf-8")
    how_many = len(document)
    document.close()
    print(f"{paper.name}: {how_many} printed pages -> {written} ({written.stat().st_size} bytes)")
    return [written]


def photograph(name, url, section=None):
    WHERE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(
            viewport={"width": 1280, "height": 1600},
            # Twice the pixels, so the small print in an API table survives
            # being looked at.
            device_scale_factor=2,
        ).new_page()
        page.goto(url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(3000)

        # PRINT MEDIA, which is the whole trick. These sites carry a nav rail,
        # a sidebar, a search bar and a floating chat button, and a full-page
        # screenshot of all that shrinks the API tables to grey noise. Their
        # print stylesheet drops the furniture and lays out the ARTICLE - the
        # same instinct as printing a page to read it on paper.
        page.emulate_media(media="print")
        page.wait_for_timeout(500)

        # Cookie walls sit over the content and photograph beautifully.
        for wording in ("Decline optional cookies", "Reject all", "Decline"):
            button = page.get_by_role("button", name=wording)
            if button.count():
                button.first.click()
                page.wait_for_timeout(1000)
                break

        stem = re.sub(r"[^\w.-]+", "-", f"{name}-{section}" if section else name)
        shot = WHERE / f"{stem}.png"

        if not section:
            # THE ACTUAL PRINT PIPELINE, which is not the same thing as
            # emulate_media(). That call only swaps the CSS media type for a
            # screenshot; page.pdf() runs Chromium's paginated print layout -
            # what a person sees in Ctrl+P - which is what drops the nav rail
            # and reflows the article to the paper width. Rasterised page by
            # page afterwards so each one arrives at a size that can be read
            # instead of a whole site scaled into grey.
            paper = WHERE / f"{stem}.pdf"
            page.pdf(path=str(paper), format="A4", print_background=True)
            browser.close()
            return extract_the_text(paper)

        # A SECTION WAS NAMED, which is the only way to arrive here - the
        # branch above returns. An `else:` taking a full-page screenshot used
        # to sit at the end of this and could never run.
        #
        # The section heading is brought to the top of a full viewport, so
        # what follows it is legible rather than a full-page thumbnail.
        heading = page.get_by_text(section, exact=True).first
        heading.scroll_into_view_if_needed()
        page.mouse.wheel(0, -80)
        page.wait_for_timeout(500)
        page.screenshot(path=str(shot))
        browser.close()
    print(f"{name}: {shot} ({shot.stat().st_size} bytes)")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    where_in_it = sys.argv[2] if len(sys.argv) > 2 else None
    for name, url in PAGES.items():
        if not which or name == which:
            photograph(name, url, where_in_it)
