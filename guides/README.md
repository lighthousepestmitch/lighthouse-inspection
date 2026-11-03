# Field Guides

Job steps and checklists for the iPad on site. Works with no signal once opened once.
Live at https://lighthousepestmitch.github.io/lighthouse-inspection/guides/

The page draws everything from JSON. To change a guide, edit its file in `data/`. You do not touch the page.

## Files

| File | What it is |
|---|---|
| `index.html`, `app.js`, `render.js`, `guides.css` | The page. `render.js` turns a guide into HTML (the tests use it too). |
| `lpc-look.css` | Colours, radius, shadow and fonts. Token names are identical to Inspection Capture's `:root`. Light only. |
| `sw.js`, `manifest.webmanifest` | Offline copy and home screen icon, same pattern as `sds/`. |
| `data/index.json` | The list on the home page. |
| `data/<id>.json` | One file per guide. |
| `tests/check.py` | Run `python3 guides/tests/check.py` before you push. |

## Add or change a guide

1. Edit or add `data/<id>.json` (id is lower case letters, numbers and hyphens).
2. Add it to `data/index.json`: `id`, `title`, `summary`, `updated` (YYYY-MM-DD, same as in the guide file), `order`, `group`.
3. Run the checks. Push.

Guide text needs no version bump (the page checks for new text whenever there is signal). If you change the page, styles, scripts or this schema, bump `BUILD` in `app.js` and `VERSION` in `sw.js` to the same new string. iPads then show "A new version is available. Tap to refresh".

## Guide file

```json
{
  "id": "cas",
  "title": "CAS treatment guide",
  "subtitle": "optional line under the title",
  "updated": "2026-11-04",
  "resetNote": "optional, replaces the default tick note",
  "sections": [ ...blocks... ]
}
```

`sections` is a list of blocks, shown in order. Every block has a `type` and may also have:

- `heading`: a big heading before the block. It also becomes a jump button (use `short` for a shorter button label).
- `title`: a smaller title on the block itself.
- `id`: optional but worth adding to checklists. Ticks are saved against it, so reordering blocks does not move ticks.

| `type` | Fields |
|---|---|
| `text` | `body`: a string, or a list of strings (one paragraph each). |
| `checklist` | `items`: strings, or `{ "text": "...", "detail": "grey line underneath" }`. Optional `note`. Each item gets a tick box. |
| `callout` | `kind`: `legal`, `never` or `note`. `body` (string or list) and/or `items` (same shape as a checklist, no tick boxes). Optional `title`. |
| `table` | `rows`: list of rows, each a list of text cells. Optional `columns` (header names). First column is bold unless `"firstColBold": false`. Use `\n` inside a cell for a line break. |
| `qa` | `items`: `{ "q": "what the customer asks", "a": "what to say" }`. Optional `title` (default "What to say if the customer asks"). |

Any text can contain `[Mitch to confirm ...]`. It shows as a visible orange marker (and prints). Leave it in until Mitch has decided, then delete it.
The business phone number and email turn into tap links by themselves. All text is escaped, so nothing in a JSON file can break the page.

## House rules for the content

Australian English. No em dashes. No licence numbers, prices or customer details in these files (this repo is public).
Where a source is unclear, leave it out or add a `[Mitch to confirm]` marker. Do not guess.

## Checks

`tests/check.py` needs only Python 3 and either Node or the Mac's built-in JavaScriptCore (`jsc`). It checks that every JSON file parses and follows the schema, every section and tick box renders, the markers show, no em dash or licence number is anywhere, the JavaScript is valid, the service worker lists every file the page uses, `BUILD` matches `VERSION`, and the colour tokens still match Capture's.
