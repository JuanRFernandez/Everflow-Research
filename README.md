# EverFlow Research Engine — Phase 0

Fills the missing contact fields in the Alpine partner workbook by fetching each
company's own website, and writes the results to a new version of the workbook
without breaking it and without inventing a single contact detail.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — where this sits in the wider plan.
- [`docs/WORKBOOK_NOTES.md`](docs/WORKBOOK_NOTES.md) — the measured facts about the
  workbook that the writer depends on.
- [`docs/PHASE0_HANDOFF.md`](docs/PHASE0_HANDOFF.md) — what Phase 0 achieved, what it
  cannot fill and why, and where Phase 1 starts.

---

## The two locations — never mixed

| | Path | Versioned by |
|---|---|---|
| **Code** | `C:/Users/rofer/GIT_repositories/Everflow-Research` | Git |
| **Data** | `G:/My Drive/03_Business-&-Projects/Everflow Experience/05_PARTNERS_AGENCIES_B2B/` | Google Drive |

The workbook is read and written in place on `G:`; Drive syncs it on its own. No
Google Drive API, no OAuth, no service account. The repo never contains the workbook
— `*.xlsx` and `data/` are gitignored, because it holds 41 named individuals, 59
phone numbers and 27 email addresses.

---

## How the workbook is edited — a fixed constraint

The owner edits the workbook **exclusively in the Google Sheets web editor**. There is
no desktop Excel on this machine, and that is not going to change.

Everything below follows from that. All of it is already handled. **None of it is a
bug, and none of it should be "fixed" again.**

| What Sheets does | What the tool does about it | Status |
|---|---|---|
| Pads PARTNERS' used range out to 1000 rows on save — 746 of them empty | The schema check compares the **data extent** (the last row holding an ID or an entity name), not the raw row count. Trailing blanks are logged and ignored; only populated rows are loaded. | **Correct as is** |
| Rewrites the whole file on open — size, mtime and internal parts all change | The write gate compares the emitted file against the input *as it was read at the start of the run*, and refuses to write rather than overwriting a file that moved underneath it. | **Correct as is** |

A sheet with fewer rows than the previous run recorded (`data/state/workbook.json`)
is refused, and a genuinely lossy write still fails the gate. There is no absolute
row floor: the first run, or a run after `--reset-state`, accepts whatever is there
and makes it the baseline. The tolerances are narrow and deliberate.

### The one operational rule

> **Never run enrichment while the workbook is open in Google Sheets.**
> Close the tab first.

If the gate refuses a write, that is the correct outcome — something changed the file
mid-run. **Stop and report it. Do not retry, and do not force.** Re-running after the
tab is closed costs nothing, because every fetched page is cached.

### Do not convert the workbook to a native Google Sheet

It would look tidier and it would break the tool permanently. Drive for desktop syncs
native Sheets as `.gsheet` **pointer files** — a few bytes of JSON holding a document
ID and no cell data at all. There would be nothing on `G:` for openpyxl to read, and
the read guard would (correctly) refuse every run.

The workbook has to stay a real `.xlsx` file. Edit it in Sheets, keep it stored as
`.xlsx`.

---

## What it does

For every PARTNERS row that has a website and is not human-owned:

1. Fetches `robots.txt`, obeys it, and adopts its `Crawl-delay` if stricter than ours.
2. Fetches the homepage; for German and Austrian sites, the **Impressum next** — it is
   legally mandatory and by far the highest-yield page in this dataset.
3. Reads `sitemap.xml` and the homepage's own links to find contact, trade, team and
   legal pages, then falls back to probing a configured path list.
4. Extracts emails (including obfuscated forms), phones (normalised to E.164),
   WhatsApp numbers, LinkedIn company pages, Instagram handles, named contacts with
   roles, and published commission terms.
5. Decides, per value, whether it is good enough to write — and records why either way.
6. Writes one new version of the workbook, verified cell by cell against the input.

---

## Setup

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
```

---

## Usage

```bash
# See what would change, without writing anything. Always start here.
uv run efe enrich --dry-run --limit 20

# A specific slice of rows (worksheet row numbers, inclusive).
uv run efe enrich --dry-run --rows 2:21

# Write a new version of the workbook to the G: folder.
uv run efe enrich

# Which file will be read, why, and whether it honours the contract. Changes nothing.
uv run efe check

# Report duplicate PARTNERS rows. Detection only — never merges. A row whose Status
# reads 'Duplicate of EFE-xxxx' counts as resolved and is not reported again.
uv run efe duplicates

# Append the rows of a PARTNERS-shaped candidates CSV (discovery output) as the next
# version: IDs, domains and names already in the sheet are left out and listed.
uv run efe promote data/out/2026-08-25_hotel_candidates.csv --dry-run
uv run efe promote data/out/2026-08-25_hotel_candidates.csv

# Prove an emitted file is faithful to its input.
uv run efe verify --against ".../..._v03.xlsx" ".../..._v04.xlsx"
```

| Flag | Effect |
|---|---|
| `--dry-run` | Reports what *would* change. Writes no workbook. |
| `--limit N` | Process at most N selected rows. |
| `--rows A:B` | Restrict to worksheet rows A–B inclusive. |
| `--workbook PATH` | Read this file instead of the highest version in `workbook_dir`. |
| `--reset-state` | Accept the chosen file as the new baseline even if it has fewer rows or a lower version than the last run recorded (`check`, `enrich`, `duplicates`). |
| `--round NAME` | Round id, written to `Round`. Default `R2-enrich`. |
| `--categories 1,2,3` | Override `selection.categories` for one run; `all` clears the filter. |
| `--resorts A,B` | Override `selection.resorts`; accent/umlaut-insensitive; `all` clears. |
| `--no-cache` | Ignore the page cache and refetch. |
| `--fresh` | Ignore the resume ledger and start the round over. |
| `--config PATH` | Use a different config file. |
| `-v` | Debug logging. |

**Resuming.** A run that dies at row 140 restarts at 140 — state lives in
`data/state/ledger-<round>.jsonl`, and the workbook is written once at the end, so a
crash mid-crawl never leaves a half-written file. A dry run keeps its own ledger
(`ledger-<round>-dryrun.jsonl`), so it never marks rows as done for the real run;
and when a run does skip rows the ledger already covers, it says so and points at
`--fresh` rather than reporting that nothing met the threshold.

---

## The config knobs that matter

Everything tunable is in [`config.yaml`](config.yaml). These are the ones worth
knowing about; the rest are vocabularies you extend as you meet new sites.

### Where things live

| Key | What it controls |
|---|---|
| `workbook_dir` | The Drive **folder**. Never a file: the highest version in it is read (below). Override per-run with `--workbook`. |
| `output_dir` | Where the new version goes. Omitted = `workbook_dir`, so the next run picks it up. Receives the versioned `.xlsx` **and nothing else**. |
| `artifacts_dir` | Everything else — run reports, decision table, CSVs, duplicates report. Local, gitignored. |
| `cache_dir`, `state_dir`, `log_dir` | Machine state, all under gitignored `data/`. |

The two are deliberately separate. A run report and a review CSV are working
output, not deliverables, and the CSVs carry contact data that has no business
syncing to a shared Drive folder. `config.sanity_check()` refuses a config that
sets them to the same directory, and every run prints both paths at the end.

### Which file is read — invariants, not constants

Nothing in `config.yaml` names a workbook version, a row count, a formula count
or a column letter; a new version never needs a config edit.

- **Resolution.** `workbook_dir` is scanned for
  `<date>_EFE_Alpine_Partner_Database_vNN.xlsx` — date with or without dashes
  (`2026-08-24_…` and `20260824_…` both count). Files marked `SUPERSEDED`, Excel
  `~$` lock files and anything under `workbook.min_plausible_bytes` (a Drive
  placeholder) are excluded. The highest `vNN` wins, newest mtime breaking a tie.
  The choice and every rejection, with its reason, are logged on every run; zero
  candidates is an error that lists the folder.
- **Contract.** `workbook.header` is the 40 PARTNERS column names, exact and in
  order — a workbook that differs is refused with a positional diff. Column
  letters are derived from that header at run time (`writable_columns`,
  `crm_columns`, `formula_columns`, `provenance_columns` are names, not letters).
  `workbook.required_sheets` must all exist (extra sheets are fine).
  `Next_Follow_Up` must hold a formula on **every** data row, except that a
  human-verified row (`Contacted = YES`) may carry a typed date instead — those
  rows are yours end to end, and `efe check` lists them.
- **Continuity.** After each successful load, `data/state/workbook.json` records
  the file, its version, its data-row count and a header hash. The next load is
  compared with it: fewer rows, a lower version or a changed header stops the run
  ("old file or half-finished sync") — `--reset-state` makes the chosen file the
  new baseline when that is deliberate.
- **Symmetric output.** The writer emits `vNN+1` against the version it actually
  read, refuses if that version already exists anywhere in the folder, never
  writes in place, and the next run's resolver reads it without any change.

`efe check` prints all of this: chosen file, rejections, sheets, header, rows,
formula column, baseline. "Blocked" is never a mystery.

### How hard it crawls — raise these only with a reason

| Key | Default | Why |
|---|---|---|
| `fetch.per_domain_delay_seconds` | `2.0` | One request per domain every two seconds. These are future partners. |
| `fetch.global_concurrency` | `5` | Domains in flight at once. |
| `fetch.max_pages_per_entity` | `8` | Page budget per row, sitemap included. |
| `fetch.max_consecutive_failures` | `4` | Abandon a domain that keeps refusing. A site answering 429 to everything is telling us to stop. |
| `fetch.respect_robots` | `true` | Leave this alone. |

### What gets written vs held for review

| Key | Default | Effect |
|---|---|---|
| `confidence.write_threshold` | `high` | Only `high` reaches the workbook; `medium` and `low` go to the review queue. |
| `confidence.high_page_kinds` | contact, impressum, trade, legal | Page kinds that earn `high`. |
| `confidence.homepage_role_email_is_high` | `true` | A role address on the entity's **own** homepage counts as high — many small operators have no `/contact` page. A third-party address in the same footer (web agency, PR firm, booking platform) is still held, and this never applies on a group domain. |
| `confidence.social_on_own_domain_is_high` | `true` | A social link on the company's own site is verifiable in one click and carries no sending risk. |
| `review.max_alternates_per_field` | `2` | Alternates kept per row per field. Anything beyond is **counted and reported**, never silently dropped. |

### The sibling-property guard

`W Verbier` → `marriott.com`. `Fouquet's Courchevel` and `Hôtel Barrière Les Neiges`
→ the same `hotelsbarriere.com`. Both Airelles properties → `airelles.com`. On a
group domain, a contact found anywhere is probably not this row's contact.

| Key | Effect |
|---|---|
| `scope.shared_domain_min_rows` | A domain used by this many PARTNERS rows is a group site. Default `2`. |
| `scope.chain_domains` | Group sites that only **one** row points at, so the row count cannot reveal them — `lek2palace.com` serves the whole Le K2 Collection but only one K2 row exists. Add domains here as you meet them. |
| `scope.needs_manual_url` | Domains that refuse automated access outright (403 to everything, or a TLS stack too old to negotiate). Rows on these are flagged, **never fetched**, and listed once in the run report. |
| `scope.name_match_min_token_ratio` | Share of a name's distinguishing tokens a page must carry to speak for it. Default `0.5`. |

On a group domain the guard applies four rules:

1. **The page must name the property** — in its URL path, `<title>`, `og:title` or
   headings. Never its body: a chain mega-menu lists every property the group owns.
2. **A group homepage identifies nothing.** It advertises every destination, so
   matching a name token there proves nothing. Only the row that *is* the group can
   take contacts from it.
3. **Group chrome is stripped** — `header`, `nav`, `footer` and cookie banners are
   removed before contact extraction, because they carry the group's central number
   and every sibling's details on every page.
4. **A social handle must name the entity** — otherwise a group footer puts
   `@airellesvenice` on the Courchevel row.

Anything rejected gets confidence `low`, stays `TBD`, and goes to the review queue.
Every run report lists **which rows the guard applied to and what it decided**.

### Duplicate detection

| Key | Effect |
|---|---|
| `dedupe.name_stopwords` | Only legal-form suffixes (`GmbH`, `Ltd`, `SARL`). Dropping generic words here would start merging different companies. |
| `dedupe.division_markers` | Words that mark a row as a separate **business unit**, not a copy: `division`, `MICE`, `apartments`, `kids`, `heli`… `Air Zermatt` and `Air Zermatt (heli-ski division)` are two sets of contacts and are never flagged. |

---

## What it will and will not write

**Never fabricates.** A value is written only if the literal characters were on a page
that was fetched. There is no code path that builds `firstname.lastname@` from an
observed pattern. Every value carries its source URL, fetch timestamp and the raw
matched substring; the writer refuses anything missing one.

**Never overwrites.** A cell holding anything other than `TBD` is left alone.

**FORM-ONLY.** When a site publishes a contact form and no email address exists on
any fetched page, `General_Email` gets the sentinel `FORM-ONLY` — a sourced fact
(the form page is the Source_URL), never a guessed address. It sits in
`empty_tokens`, so a real address found on a later run replaces it. Search, login
and newsletter forms do not count.

**Targeting.** `selection.categories` / `selection.resorts` in `config.yaml` scope a
round (currently: categories 1–3 across the GaPa · Arlberg · Kitzbühel · Innsbruck ·
Zell–Kaprun corridor). Rows outside the targets are skipped with an explicit reason;
`--categories all --resorts all` runs everything.

**Promotion.** `efe promote <csv>` appends discovery candidates as new rows after
the last data row — proven empty across every column first, so a note typed on one
of the rows Sheets pads the sheet with stops the run instead of vanishing. IDs must
be `EFE-dddd` above the sheet's highest; a domain (exact or registrable) or a
normalised name already in the sheet leaves the candidate out, listed with the
reason. Each new row gets the column's dominant `Next_Follow_Up` formula. What
promotion cannot make true, it says: the DASHBOARD's cached totals in the output
reflect the input version until Sheets recomputes them on open, and rows beyond
the DASHBOARD's ranges (or the stale autofilter / dropdown ranges) are reported.

**Never touches the CRM.** Columns Z–AJ (`Contacted` … `Next_Action`) are yours.
`AC` / `Next_Follow_Up` is a live formula, preserved with its cached result.

**The 18 gold rows** (`Contacted = YES`) are skipped entirely — not fetched, not
written.

**GDPR.** Role-based corporate addresses (`info@`, `sales@`) go to `General_Email` and
`Sales_B2B_Email`. A named individual's address is *never* written to PARTNERS — it is
recorded in `CHANGELOG_DETAIL` and the review CSV tagged `data_class=personal_named`,
so you decide per campaign. Names and roles still go to `Contact_Person_Name` /
`Contact_Person_Role` when published. Nothing behind a login or paywall is fetched.

### General_Email vs Sales_B2B_Email

The local part decides, then the page. `partnerships@example-agency.com` → **J**
("contains the trade/B2B token `partnerships`, sales priority 7");
`singapore.enquiries@example-agency.com` → **I** (role token `enquiries`), both off the
same trade page (domain anonymised — real partner addresses stay in the workbook, never in the repo). An address whose local part matches no known role token is never
written — it goes to review, and you widen `email.sales_local_parts` or
`email.general_local_parts` if it should have been.

---

## Output

**To the Drive folder (`output_dir`) — the workbook, and only the workbook:**

- `YYYY-MM-DD_EFE_Alpine_Partner_Database_vNN.xlsx` — one above the version that
  was read, never an overwrite. A row is appended to `CHANGELOG` after its last real
  entry, and `CHANGELOG_DETAIL` (a required sheet with the 15-column audit layout; rows are
  appended after its last real entry)
  logs every cell change *and* every held-back candidate with old value, new value,
  source URL and confidence.

**To `artifacts_dir` (local, gitignored) — everything else:**

- `..._run_report.md` / `.json` — rows processed, fields filled, fields still TBD,
  failures with reasons, domains abandoned, rows needing a manual URL, the
  shared-domain guard table, Round-1 ledger domains revisited and why.
- `..._changes.csv` — every cell written, with its reason.
- `..._review_queue.csv` — every held candidate and why.
- `dry_run_<timestamp>.md` — the decision table. One row per decision: Entity, Field,
  Old, New, Confidence, Source URL, Decision reason; written and held-back alike.
- `duplicates_<timestamp>.md` — duplicate rows with both CRM columns side by side and
  a recommended keep/drop. **Detection only, never a merge.**

Both destinations are printed at the end of every run, so it is always clear which
file went where.

### The fidelity gate

openpyxl rewrites the whole workbook, so before an output is accepted it is compared
against the input cell by cell: every formula at its identical address, the data
validations, the autofilter, freeze panes, column widths, number formats, and every
value outside the intended write set. openpyxl also discards every formula's cached
result; those are reinjected from the input, which is sound because no column this
tool writes feeds any formula (`assert_no_precedents_touched` enforces that premise).
**If the gate fails, the candidate file is deleted and the input is untouched.**

---

## Discovery (ad hoc): `scripts/discover_hotels.py`

The enricher fills fields on rows that exist. Finding *new* rows is discovery, which
ARCHITECTURE.md §7 places in Phase 2 behind a repeatable `resort_directory` plugin.
`scripts/discover_hotels.py` is the one-off precursor: it reads the official
tourism-board pages of the hotel corridor (GaPa, Arlberg, Kitzbühel, Seefeld,
Innsbruck, Stubai, Zell-Kaprun) and emits **candidates**, never workbook rows.

```bash
uv run python scripts/discover_hotels.py
# -> data/out/<date>_hotel_candidates.csv  (+ _detail.csv with the discards and why)
```

Rules it shares with the enricher: nothing is invented. A candidate carries only what
the source page literally publishes — name, the website it links to, the stars it
states, the postcode line it prints — plus the page URL as `Source_URL`. On pages that
list several properties a link only counts as a property's website when a
distinguishing token of the name appears in the domain; on single-property fiches the
link the page labels as the website wins, then name affinity, and vendor credits
(architects, agencies, builders) are never taken. Otherwise the row is marked
`[SIN WEB]`. `Segment_Tier` comes only from published stars (5* → luxury, 4*S →
premium, 4* → mid-premium); anything else stays TBD. Candidates already in PARTNERS
(by domain or folded name) are dropped and listed in the detail file, as are internal
duplicates (same domain, or a contained name within the same resort).

Two rules keep boards from leaking their own data into rows. The property's town is
the **first** postcode line in document order after nav/footer chrome is removed — a
page whose own address lies outside the corridor is dropped rather than rescued by the
board's footer address (Tirol Werbung's "6020 Innsbruck" once turned Sölden into
Innsbruck). And names lose their editorial framing only where it is unambiguous:
"Wellness im Hotel X", "Restaurant im Landhotel Y", "Seminarraum Hotel Z",
"4-Sterne-Hotel …", a trailing " - Après Ski"; everything else stays verbatim for the
human to judge.

The accommodation *search* widgets on these boards are JavaScript (Feratel/Deskline)
and yield nothing — so do Lech-Zürs' per-property fiches, Stubai's hotel list and
Wilder Kaiser's Next.js pages; the script uses editorial pages, static property fiches
(tirol.at, alpenwelt-karwendel.de, vorarlberg.travel, zugspitz-region.de) and, for
Zell am See–Kaprun, the POI fiches hosted by hotels (day spa, restaurant, seminar
room), which print the hotel's own address and a labelled website link. IDs start at
EFE-0359 and `Round` is `R3-discovery`. Promotion is human: paste the rows you approve
into the sheet (as `.xlsx`, not a native Sheet), download, and run `efe enrich` to
fill contacts.

## Tests

```bash
uv run pytest

# Prove the suite needs no network at all:
uv run pytest -p tests.no_network
```

Fixtures are hand-written synthetic HTML with invented data. Real fetched pages are
never committed — they contain named individuals.
