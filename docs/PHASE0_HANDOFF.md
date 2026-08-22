# Phase 0 — handoff

Closed 2026-08-22. Input `…_v03.xlsx`, output `2026-08-21_EFE_Alpine_Partner_Database_v04.xlsx`.

Phase 0 was scoped in [`ARCHITECTURE.md`](ARCHITECTURE.md) §7 as exactly one thing:
**one workbook in, one enriched workbook out.** That is what was built. No SQLite, no
`Source` plugin interface, no PPS scoring.

---

## What it filled

376 cells across **133 of 253 rows**, from 476 HTTP requests (1,596 pages served from
cache on the replay). Every value carries a source URL and the date the page was
actually fetched.

| Col | Field | Before | After | Gain |
|---|---|---:|---:|---:|
| I | General_Email | 27 | 87 | **+60** |
| J | Sales_B2B_Email | 1 | 10 | **+9** |
| K | Phone | 59 | 163 | **+104** |
| L | WhatsApp | 38 | 62 | **+24** |
| M | Contact_Person_Name | 41 | 56 | **+15** |
| N | Contact_Person_Role | 2 | 17 | **+15** |
| O | LinkedIn_URL | 0 | 64 | **+64** |
| P | Instagram_Handle | 70 | 152 | **+82** |
| U | Commission_or_Partner_Terms | 0 | 3 | **+3** |

A further **610 candidates were held for review** — real, published values that did
not clear the bar — and 1,254 more alternates were found beyond the per-field cap of
2 and counted rather than silently dropped. They are in
`…_v04_review_queue.csv`.

**Nothing was invented.** There is no code path that constructs an address from an
observed pattern. Every written value carries the source URL, the fetch timestamp and
the raw matched substring; the writer refuses anything missing one.

### What was not touched

- The **18 gold rows** (`Contacted = YES`) — never fetched, never written. Verified:
  0 cells changed on those rows.
- **CRM columns Z–AJ** — verified: 0 cells changed.
- **All 491 formulas** — identical at identical addresses, with their cached results
  preserved, so `DASHBOARD` still reads 253 / 18 and `RESORTS_SBI` still ranks
  without needing a recalculation.

---

## What it cannot fill, and why

**48 rows were never attempted**, for three distinct reasons:

| Reason | Rows | What would unblock it |
|---|---:|---|
| Human-verified (`Contacted = YES`) | 18 | Nothing — deliberate. |
| No `Website_URL` at all | 22 | Discovery, not enrichment. Phase 2. |
| Domain refuses automated access | 8 | A property-level URL (below). |

### The 8 rows needing a manual URL

These domains answer 403 to every request, or present a TLS stack too old to
negotiate. There is no polite workaround and an impolite one was explicitly out of
scope — these are future partners. They are flagged in `scope.needs_manual_url` and
**not fetched at all**, so no request is wasted on them each run.

| Row | Entity | Current `Website_URL` |
|---:|---|---|
| 4 | Cheval Blanc Courchevel | `chevalblanc.com` |
| 6 | L'Apogée Courchevel | `oetkercollection.com` |
| 11 | Six Senses Residences Courchevel | `sixsenses.com` |
| 12 | Four Seasons Hotel Megève | `fourseasons.com` |
| 15 | The Chedi Andermatt | `thechediandermatt.com` |
| 16 | Six Senses Crans-Montana | `sixsenses.com` |
| 17 | W Verbier | `marriott.com` |
| 28 | Oetker Collection (group partnerships) | `oetkercollection.com` |

Replace each with the property's own page and it enriches normally on the next run.
Note that seven of the eight are *also* group domains, so even if they answered they
would face the sibling guard — a property-level URL fixes both problems at once.

### 42 more domains abandoned mid-run

The fetcher stops after 4 consecutive failures on a domain, because a site answering
429 to everything is telling us to stop.

| Cause | Domains |
|---|---:|
| HTTP 403 — bot blocking | 19 |
| TLS / certificate chain | 8 |
| DNS / connection refused | 6 |
| Timeout | 3 |
| HTTP 429 — rate limited | 2 |
| Other | 4 |

The **TLS group is worth a second look**: those are small operators whose servers do
not serve their intermediate certificate, or negotiate a key too small for a modern
client. `fetch.verify_tls: false` exists in config and would recover them — it is
left `true` because turning off certificate verification to scrape a partner's site
is a poor trade, but it is your call, not the tool's.

### The sibling-property ceiling

The guard fired on **49 rows** sitting on a group or chain domain. Of those, 32 found
at least one page that genuinely named the property, 12 never did, and 5 fetched
nothing. **21 of the 49 gained nothing at all.**

This is a data problem, not a crawling problem: when `Website_URL` points at
`marriott.com`, no amount of crawling establishes which contact belongs to W Verbier.
The remedy is a property-level URL in column H. See the guard table in
`…_v04_run_report.md` for the row-by-row verdict.

### Why `Sales_B2B_Email` and `Commission_or_Partner_Terms` stayed thin

+9 and +3 respectively, against +104 phones. This is a finding, not a shortfall:
**luxury Alpine properties largely do not publish trade terms or a trade address.**
`_GAPS_ROUND2` row 3 predicted exactly this ("Most do not publish. Requires direct
outreach."). Where a trade page existed the tool found it — `partnerships@example-agency.com`
routed to column J off Scott Dunn's trade page, alongside
`singapore.enquiries@example-agency.com` to column I. (Domain anonymised here — real partner addresses stay in the workbook, not in the repo.) There simply were not many.

---

## Duplicate rows — 7 pairs, reported, not merged

Six are the same company entered twice with different accent spellings; one
(`La Casa del Viaje`, rows 209/232) differs by an en-dash as well.

| Rows | Entity | Recommendation |
|---|---|---|
| 170 / 235 | Matuete / Matueté | keep 170 — it has live CRM state |
| 172 / 237 | TTW Group | keep 172 — live CRM state |
| 183 / 236 | TM Travel Tailor Made | keep 183 — live CRM state |
| 201 / 231 | NUBA Travel | keep 201 (arbitrary; neither is worked) |
| 209 / 232 | La Casa del Viaje – Querétaro | keep 209 (different domains — check) |
| 211 / 233 | Julia Tours México | keep 211 |
| 213 / 234 | Viajes Bojórquez (Matriz) | keep 234 — fuller record |

**Nothing was merged.** In three pairs one row carries outreach history the other does
not, so deleting the wrong one loses real work. Run `efe duplicates` for the full
side-by-side. Business-unit rows — `Air Zermatt (heli-ski division)`,
`Cimalpes (apartments)`, the three Scott Dunn rows, `Powder Byrne MICE`,
`Heli Bernina (heli-ski)` — are deliberately **not** flagged.

---

## What was built

~6,200 lines of source, ~3,000 of tests, **243 tests passing with the network
blocked**.

```
src/efe/
  cli.py          efe enrich | check | verify | duplicates
  config.py       every tunable, validated on load
  models.py       records shaped as the Phase-1 entity_field table
  pipeline.py     orchestration, resumable ledger, confidence decisions
  report.py       run report, decision table, review queue
  dedupe.py       accent-insensitive duplicate detection
  workbook/       guarded read, guarded write, the fidelity gate, xlsx surgery
  fetch/          cache, robots, rate limit, client, page discovery
  extract/        emails, phones, socials, persons, terms, impressum, scope, classify
```

### The three things worth knowing

**1. The fidelity gate is the load-bearing part.** openpyxl rewrites the whole
workbook, so every emitted file is compared against the input cell by cell before it
is accepted — 491 formulas, 5 data validations, autofilter, freeze panes, column
widths, number formats, and every value outside the intended write set. If it fails,
the candidate is deleted and the input is untouched.

It earned its keep. Mid-run, the workbook was re-exported through Google Sheets and
changed underneath us; the gate refused the write rather than overwriting. It also
exposed a weakness in itself — dropped parts were whitelisted by *filename*, which
would have accepted a drawing containing a real chart. Drawings are now judged by
content. See [`WORKBOOK_NOTES.md`](WORKBOOK_NOTES.md).

**2. Nothing below `high` confidence is written.** `high` means an official contact,
Impressum, legal or trade page on the entity's own domain, scope guard satisfied.
Everything else goes to the review queue with the reason attached. That is why 610
candidates were held: the bar is doing its job.

**3. Named-individual emails never reach PARTNERS.** Only role-based corporate
addresses reach columns I and J. A personal address is recorded in `CHANGELOG_DETAIL`
and the review CSV tagged `data_class=personal_named`, for you to place per campaign.
Names and roles still populate M and N when published.

### Known rough edges

- **A full run is slow.** This one took ~12.5 hours wall-clock for 205 rows, dominated
  by dead and blocking domains: each bad page costs up to 3 retries × a 20-second
  read timeout, and only 5 domains are in flight. Successful rows are fast (48 rows in
  the first 3 minutes). If this becomes painful, the lever is a shorter
  `fetch.timeout_seconds` for retries, not more concurrency.
- **`robots.txt` is not cached between runs**, so every re-run re-fetches it per
  domain. Harmless but wasteful.
- **Failed pages are deliberately not cached** — a 429 is a moment in time, not a fact
  about a page — which makes re-runs re-attempt every failure.

---

## Where Phase 1 starts

Phase 1 in `ARCHITECTURE.md` §7 is the SQLite store plus `export`/`import` with the
column-ownership split. Three seams were built for it deliberately:

**1. The ledger is already `entity_field`.** `data/state/ledger-R2-enrich.jsonl` holds
1,385 append-only records carrying exactly
`(entity_id, field, value, confidence, source_url, fetched_at, round_id)` — the
Phase-1 table, field for field. The import is a replay, not a migration.

**2. `fetch/` and `extract/` never import `workbook/`.** A Phase-2 `Source` plugin
reuses the whole fetcher, rate limiter, robots cache and extractor set unchanged. Row
loading already yields a struct that maps 1:1 to the planned `Candidate`.

**3. The CLI is `efe <verb>`.** `export`, `import`, `round start` and `report` slot in
beside `enrich`, `check`, `verify` and `duplicates` without restructuring.

### What Phase 1 should fix first

1. **Column H is the bottleneck, not the crawler.** 30 rows have no URL, 8 have an
   unusable one, and 21 more sit on group domains that cannot identify them. That is
   ~59 rows — a quarter of the sheet — blocked on one column. A `resort_directory`
   source (ARCHITECTURE §3, first in the build order) is the cheapest fix.
2. **Entity resolution.** The 7 duplicate pairs need deciding before a SQLite import,
   or they become 7 duplicate entities with split provenance.
3. **Then outreach.** Per the governing principle: no new discovery round starts until
   every Priority-5 row from this one has been contacted. The tool now gives you 87
   general addresses, 10 trade addresses and 163 phone numbers to work with.

---

## Files this produced

**On the Drive — the workbook only.** `output_dir` receives the versioned `.xlsx` and
nothing else; every process artifact stays local in `artifacts_dir`
(`./data/out`, gitignored). Run reports and review CSVs are working output, not
deliverables, and the CSVs carry contact data that should not sync to a shared
folder. Both paths are printed at the end of every run.

- `2026-08-21_EFE_Alpine_Partner_Database_v04.xlsx` — with a new `CHANGELOG_DETAIL`
  sheet, 1,385 rows logging every cell change *and* every held-back candidate.

In `data/out/`:

- `…_v04_run_report.md` / `.json` — totals, skips with reasons, failures, domains
  abandoned, rows needing a manual URL, the shared-domain guard table, ledger
  revisits.
- `…_v04_changes.csv` — every cell written, with its reason.
- `…_v04_review_queue.csv` — all 610 held candidates and why.
- `dry_run_<timestamp>.md` — the decision table.
- `duplicates_<timestamp>.md` — the merge decision sheet.

## After the handoff

Two things changed in the folder once v04 landed, both recorded here so the next
person is not surprised:

- **v03 was archived** to `99_ARCHIVE/2026-08-21_EFE_Alpine_Partner_Database_v03_SUPERSEDED.xlsx`,
  so `config.yaml`'s `workbook_path` now points at v04.
- **v04 was opened and re-saved**, which padded PARTNERS' used range out to 1000 rows
  with 746 empty ones. The schema check now compares the *data* extent — the last row
  holding an ID or an entity name — and logs trailing blanks rather than failing on
  them. A genuinely short sheet still fails.
