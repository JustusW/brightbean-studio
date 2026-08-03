# Third-party front-end libraries, served by us

Everything the browser loads comes from this application. Nothing on a page is
fetched from a public CDN.

## Why

The browser suite drives real pages, and a page that pulls a script off
`cdn.jsdelivr.net` makes that suite depend on the public internet: a CI job
then fails when a CDN is slow, an edge node is down, or a runner has no
outbound network - and the failure reads as a broken product. It also meant
the Content Security Policy had to allow a third-party origin for
`script-src`, `style-src` and `connect-src`, so anything served from that
origin could execute on every page of this application.

Both of those are now gone: the policy allows `'self'` and the files are here.

## What is here, and where it came from

Fetched from the jsDelivr npm mirror on 2026-08-03. Byte sizes are what landed
on disk, so a truncated download is visible rather than silent.

| file | version | upstream |
|---|---|---|
| `flatpickr.min.css` | 4.6.13 | `https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css` (16166 bytes) |
| `flatpickr.min.js` | 4.6.13 | `https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js` (50679 bytes) |
| `chart.umd.min.js` | 4.4.6 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js` (205889 bytes) |
| `swagger-ui.css` | 5.32.12 | `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css` (178977 bytes) |
| `swagger-ui-bundle.js` | 5.32.12 | `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js` (1555039 bytes) |

The swagger templates asked for `@5`, a FLOATING major - whatever 5.x jsDelivr
resolved that day. Pinned above at what it actually resolved to, because the
bytes in this directory are now the only thing that runs and a range no longer
describes them.

`cropper.min.css` and `cropper.min.js` were vendored before this file existed
and their version is NOT recorded anywhere. Left as they are rather than
guessed at; whoever next touches the cropper should establish which release
these are and add them to the table.

## Upgrading one

Download the new file over the old one, update its row above, and run the
browser suite - these are loaded by real pages, so a broken upgrade shows up
as a page that stops working rather than as a warning.

Do not edit these files. A local change to a minified bundle is invisible in
review and lost by the next upgrade.
