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

# Check the workbook is readable and matches the expected schema; changes nothing.
uv run efe check

# Report duplicate PARTNERS rows. Detection only — never merges.
uv run efe duplicates

# Prove an emitted file is faithful to its input.
uv run efe verify --against ".../..._v03.xlsx" ".../..._v04.xlsx"
```

| Flag | Effect |
|---|---|
| `--dry-run` | Reports what *would* change. Writes no workbook. |
| `--limit N` | Process at most N selected rows. |
| `--rows A:B` | Restrict to worksheet rows A–B inclusive. |
| `--workbook PATH` | Override `workbook_path` for one run. |
| `--round NAME` | Round id, written to column AM. Default `R2-enrich`. |
| `--no-cache` | Ignore the page cache and refetch. |
| `--fresh` | Ignore the resume ledger and start the round over. |
| `--config PATH` | Use a different config file. |
| `-v` | Debug logging. |

**Resuming.** A run that dies at row 140 restarts at 140 — state lives in
`data/state/ledger-<round>.jsonl`, and the workbook is written once at the end, so a
crash mid-crawl never leaves a half-written file.

---

## The config knobs that matter

Everything tunable is in [`config.yaml`](config.yaml). These are the ones worth
knowing about; the rest are vocabularies you extend as you meet new sites.

### Where things live

| Key | What it controls |
|---|---|
| `workbook_path` | The input workbook. Override per-run with `--workbook`. |
| `output_dir` | The Drive folder. Receives the versioned `.xlsx` **and nothing else**. |
| `artifacts_dir` | Everything else — run reports, decision table, CSVs, duplicates report. Local, gitignored. |
| `cache_dir`, `state_dir`, `log_dir` | Machine state, all under gitignored `data/`. |

The two are deliberately separate. A run report and a review CSV are working
output, not deliverables, and the CSVs carry contact data that has no business
syncing to a shared Drive folder. `config.sanity_check()` refuses a config that
sets them to the same directory, and every run prints both paths at the end.

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

- `YYYY-MM-DD_EFE_Alpine_Partner_Database_vNN.xlsx` — a new version, never an
  overwrite. A row is appended to `CHANGELOG`, and a new `CHANGELOG_DETAIL` sheet logs
  every cell change *and* every held-back candidate with old value, new value, source
  URL and confidence.

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
against the input cell by cell: all 491 formulas at identical addresses, the 5 data
validations, the autofilter, freeze panes, column widths, number formats, and every
value outside the intended write set. openpyxl also discards every formula's cached
result; those are reinjected from the input, which is sound because no column this
tool writes feeds any formula (`assert_no_precedents_touched` enforces that premise).
**If the gate fails, the candidate file is deleted and the input is untouched.**

---

## Tests

```bash
uv run pytest

# Prove the suite needs no network at all:
uv run pytest -p tests.no_network
```

Fixtures are hand-written synthetic HTML with invented data. Real fetched pages are
never committed — they contain named individuals.
