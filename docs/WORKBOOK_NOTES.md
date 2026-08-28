# Workbook notes — measured, not assumed

Everything here was read off
`2026-08-21_EFE_Alpine_Partner_Database_v03.xlsx` on 2026-08-21; the counts below
are that snapshot's (v05, 2026-08-24, holds 277 data rows and 10 sheets). The writer
depends on the *structural* facts — the ordered header, the required sheets, a
formula in `Next_Follow_Up` on every data row, the formula dependency graph — and
`efe check` re-asserts those on every run (see README, "Which file is read"). Row
and formula counts are not asserted against config any more; the row count is only
compared with the previous run's, and going backwards is refused.

---

## Structure

| | |
|---|---|
| Sheets | `READ_ME, DASHBOARD, PARTNERS, RESORTS_SBI, PRICING_BENCH, REGULATORY, _SOURCES, _GAPS_ROUND2, CHANGELOG` — plus `CHANGELOG_DETAIL` since v04 (required from v05 on) |
| PARTNERS | `A1:AM254` — 39 columns, 253 data rows (v05: 277 rows, padded to ~1000 by Google Sheets; the padding is ignored; v07 adds a 40th column, `Material_Sent`) |
| Autofilter | `A1:AM254` |
| Freeze panes | `C2` |
| Data validations | 5 — `T2:T254` (Y/N/Unknown), `Y2:Y254` (1–5), `Z2:Z254` (NO/YES), `AD2:AH254` (X), `AI2:AI254` (21 statuses) |
| Defined names | 3 sheet-local `_xlnm._FilterDatabase` (PARTNERS, RESORTS_SBI, _SOURCES) |
| Charts / images / pivots | none — the 3 `xl/drawings/drawing*.xml` parts are empty LibreOffice stubs |
| Last writer | LibreOffice 24.2.7.2 (`docProps/app.xml`); created by openpyxl |

## Formulas — 491, and where they are

| Sheet | Count | What |
|---|---|---|
| DASHBOARD | 55 | `COUNTIF` / `COUNTIFS` over PARTNERS, plus `SUM` totals |
| PARTNERS | 253 | all in column **AC**: `=IFERROR(AA{r}+AB{r},"")` (`Next_Follow_Up`) |
| RESORTS_SBI | 183 | `SUMPRODUCT` against the editable weights row 5, `MAX` row 6, `RANK` |

**The dependency fact the writer is built on:** the only cross-sheet references in
the entire workbook are DASHBOARD → PARTNERS, 74 of them, and they touch only
`$C` (Category), `$G` (Country), `$Y` (Priority_Score), `$Z` (Contacted) and
`$AI` (Status). RESORTS_SBI is completely self-contained.

None of those five columns is writable by this tool. `PARTNERS!AC` depends on `AA`
and `AB`, which are CRM columns and equally off-limits. So **no write this tool
makes can change any formula's result anywhere in the workbook.**

`writer.assert_no_precedents_touched` enforces that premise on every enrichment
run. Promotion is the one deliberate exception: new rows fall inside the
DASHBOARD's COUNTIF ranges, so the reinjected DASHBOARD totals reflect the input
version until Sheets recomputes them on open — stated in the CHANGELOG entry and
the promotion report, never silent. Those
seven columns are listed by name as `workbook.formula_precedents` in `config.yaml`
(`Category, Country, Priority_Score, Contacted, Status, Contact_Date,
Follow_Up_Days`); if someone later adds one of them to `writable_columns`, the run
stops rather than silently preserving a stale DASHBOARD total.

## Cached formula results — the one thing openpyxl destroys

All 491 formula cells carry a cached `<v>` result (written by LibreOffice). An
openpyxl `load_workbook` → `save` round-trip replaces every one with an empty
`<v></v>`, so a reader using `data_only=True` — Google Sheets on import, pandas, any
downstream script — sees `None` until Excel recalculates.

`xmlutil.reinject_cached_values` copies them back from the input after the save. That
is only sound because of the dependency fact above.

### Round-trip fidelity spike, 2026-08-21

`load_workbook` → `save`, zero edits, input vs output:

```
[OK ] sheet names/order (9)          [OK ] freeze panes
[OK ] formulas (491)                 [OK ] column dimensions
[OK ] data validations               [OK ] number formats
[OK ] autofilter refs                [OK ] all cell values (11845)
[OK ] defined names
cached <v>: IN=491  OUT=0            <- the only loss, repaired by reinjection
```

Parts openpyxl drops, all harmless and all whitelisted in `verify.BENIGN_DROPPED_PARTS`:

| Dropped | Why it does not matter |
|---|---|
| `xl/drawings/drawing{1,2,3}.xml` | empty `<xdr:wsDr/>` stubs, 299 bytes each, no content |
| `xl/worksheets/_rels/sheet{3,4,7}.xml.rels` | relationships to those empty drawings |
| `xl/sharedStrings.xml` | openpyxl emits inline strings instead; all 11,845 values verified identical |
| `docProps/custom.xml` | an empty `<Properties/>` element |

**Conclusion: the openpyxl path is safe for this workbook**, with cached-value
reinjection and the fidelity gate. The surgical zip-level writer described in the
plan was not needed and is not built.

## What a Google Sheets save looks like from the tool's side — 2026-08-21, 19:39

Partway through the first full enrichment run, `v03` on the Drive changed underneath
us: 105,808 bytes became 126,013. The fidelity gate caught it on the write, refused
to emit, and deleted the candidate.

**This was not an incident.** The owner edits the workbook exclusively in the Google
Sheets web editor (see [`PHASE0_HANDOFF.md`](PHASE0_HANDOFF.md)), and Sheets rewrites
the whole file on open. The workbook simply had a tab open while a job was running.
The write-up is kept because it is the clearest record of *what that rewrite does to
the file*, which is worth knowing when reading the fidelity claims below.

**What the rewrite changes:** `docProps/app.xml` (LibreOffice's signature) disappears,
an `xl/metadata` part with no `.xml` extension appears carrying a Google blob
(`en_US`, `America/Los_Angeles`, a default font), every sheet gains its own `_rels`,
and the three empty drawing stubs become nine. A later save also padded the used range
out to 1000 rows. A genuine Sheets *export* (v07, 2026-08-26) also carries
`xl/persons/person.xml`, the threaded-comment author list; openpyxl drops it, and
the gate accepts that only while the workbook holds no `xl/threadedComments/` part.

**What was verified afterwards, before trusting the file again:**

| | Before | After |
|---|---|---|
| PARTNERS | `A1:AM254` | `A1:AM254` |
| Formulas | 491 (55 / 253 / 183) | 491 (55 / 253 / 183) |
| Data validations | 5 | 5 |
| Autofilter | `A1:AM254` | `$A$1:$AM$254` (cosmetic) |
| Freeze panes | `C2` | `C2` |
| `Contacted = YES` | 18 rows | the same 18 row numbers |
| Round tags | R1 161 / Pre-existing 92 | R1 161 / Pre-existing 92 |
| Cached results | present | present |

**No data was lost.** The re-export is cosmetically different and semantically
identical.

**What it changed in the code:** the gate had been whitelisting dropped parts by
*filename*, including `xl/drawings/drawingN.xml` — justified when they were 299-byte
empty stubs, but a filename match would equally have accepted a drawing containing a
real chart or image. Drawings are now judged by **content**: `verify.snapshot` counts
`twoCellAnchor` / `oneCellAnchor` / `absoluteAnchor` elements, and a dropped drawing
is benign only when that count is zero. All nine of Google's drawings hold zero
anchors, so dropping them is safe; a drawing with an object now fails the gate.

`xl/metadata` is whitelisted explicitly, with the reasoning in the code: it is
Google's private round-trip blob, not the OOXML `xl/metadata.xml` cell-metadata part,
and Excel ignores it.

**The operational rule:** never run enrichment while the workbook is open in Google
Sheets. The gate compares the emitted file against the input *as it was read at the
start of the run*, so a file that moves underneath a job is caught rather than
silently overwritten. On a refusal, stop and report — do not retry and do not force.
Re-running with the tab closed costs nothing because every page is cached.

## Data as of v03

| | |
|---|---|
| Data rows | 253 |
| `Round = R1` / `Pre-existing` | 161 / 92 |
| `Contacted = YES` | **18** — as of v03; since 2026-08-29 these rows are enriched like any other (ownership is per column) |
| Rows with a usable `Website_URL` | 223 (206 unique domains) |
| Rows the enricher selects | **213** (149 R1 + 64 Pre-existing) |
| Rows skipped | 40 — 18 gold, 22 without a website |

Fill state of the target columns before any run:

| Col | Field | Filled | TBD |
|---|---|---|---|
| I | General_Email | 27 | 226 |
| J | Sales_B2B_Email | 1 | 252 |
| K | Phone | 59 | 194 |
| L | WhatsApp | 38 | 215 |
| M | Contact_Person_Name | 41 | 212 |
| N | Contact_Person_Role | 2 | 251 |
| O | LinkedIn_URL | 0 | 253 |
| P | Instagram_Handle | 70 | 183 |
| U | Commission_or_Partner_Terms | 0 | 253 |

`Date_Verified` (AL) is stored as **text** `"2026-08-21"` with number format
`General`, not as a date. The writer writes text to match.

## Group and chain domains

16 domains are shared by more than one row, covering 33 rows; several more rows point
at a global chain rather than the property:

```
scottdunn.com x3   airelles.com x2      oetkercollection.com x2   hotelsbarriere.com x2
sixsenses.com x2   cimalpes.com x2      powderbyrne.com x2        air-zermatt.ch x2
tyrol.com x2       helibernina.ch x2    matuete.com x2            ttwgroup.com x2
tmtravel.com.br x2 nuba.com x2          juliatours.com.mx x2      viajesbojorquez.com x2
```

plus single-row chain domains: `marriott.com` (W Verbier), `fourseasons.com`,
`aman.com`, `rosewoodhotels.com`, `chevalblanc.com`.

The scope guard matches against the page's **identity text only** — URL path, `<title>`,
`og:title`, `h1`/`h2`/`h3`. Not the body: a chain site's navigation lists every
property the group owns, so body matching would let one group contact page claim to
be about any of them.

## Near-duplicate rows (for Phase-1 dedup, not fixed here)

The v03 recovery merge left several entities twice, distinguishable only by accents:

`Matuete` / `Matueté` · `Julia Tours Mexico` / `Juliá Tours México` ·
`Viajes Bojorquez (Matriz)` ×2 · `TTW Group` ×2 · `TM Travel Tailor Made` ×2 ·
`NUBA Travel` / `NUBA (Nuba Travel)` · `Cimalpes` / `Cimalpes (apartments division)`

Every run report lists these. Merging them is Phase-1 entity-resolution work
(`ARCHITECTURE.md` §3), deliberately not attempted by Phase 0.

## `_SOURCES` and the revisit question

223 domains, all flagged `Exclude_Next_Round = YES`. **99 of them are PARTNERS
website domains.**

That flag governs **discovery** (Phase 2): do not go looking for new entities there
again. It does not govern **enrichment**. Round 1 visited those domains to find
*who exists*, not to extract contact details — which is precisely why 226 rows still
read `TBD`. The enricher revisits them on purpose and logs every revisit, with that
reason, in the run report.

## One observation, not acted on

`Next_Follow_Up` (`AC`) is `=IFERROR(AA{r}+AB{r},"")`. On an uncontacted row `AA` is
blank and `AB` is 14, so the formula evaluates to `14` — which Excel renders as
1900-01-14 under a date format. It is harmless while `Contacted = NO`, and it is your
formula, so nothing here touches it. Flagging it in case the DASHBOARD ever counts on
that column.

## The v08 incident and the v09 corrective release — 2026-08-27

**What happened.** On 2026-08-26 `efe promote` emitted v08 (343 rows) at 12:02. That
evening 66 South Tyrol ski schools were added to the same file by hand, at rows
345–410, under IDs `EFE-0344..0409`. Rows 279–344 already held the 66 promoted hotels
under `EFE-0359..0439`, so **39 IDs ended up on two different businesses**, and the
file kept the name `..._v08.xlsx` while its bytes and row count changed.

**Why nothing complained.** Three blind spots, all now closed:

| Blind spot | Now |
|---|---|
| Nothing counted distinct IDs | `SchemaReport.duplicate_ids`; `efe check` prints `ids WARN` |
| Range drift was only reported by `efe promote`, after the fact | `efe.workbook.ranges` measures it; `efe check` prints a `Ranges` block |
| `data/state/workbook.json` recorded `file_sha256` but never compared it | `state.continuity_notices`; `efe check` prints `Continuity → DRIFT` |

**Why the ski schools moved and not the hotels.** `CHANGELOG_DETAIL` already names
`EFE-0359..0439` against the promote run of 2026-08-26, so those IDs have an audit
trail pointing at the hotels. The ski schools had none. They moved to `EFE-0440..0505`,
which also frees `EFE-0281..0358` again — that block is reserved for
`data/out/2026-08-24_crm_merge.csv` (78 rows) and is passed to `efe fixup --reserve`
so the reservation is checkable rather than remembered.

**What v09 changed**, verified three ways (`efe verify --expect-plan`, an independent
cell-by-cell diff, and `efe check`): exactly 255 PARTNERS cells — 66 `ID`, 148 `Phone`,
34 `WhatsApp`, 7 `Sales_B2B_Email` — 0 unplanned, 0 planned-but-missing. 409 rows, 409
distinct IDs. All 638 formulas and all 638 cached results identical.

**What v09 deliberately did not change.** The autofilter (`$A$1:$AM$251`), the five
data validations (row 251) and the DASHBOARD's ranges (row 400) are still stale, and
`12. Ski Schools & Instructor Supply` is on no DASHBOARD row. Extending a DASHBOARD
range rewrites a formula whose cached result is reinjected from the input, so the file
would ship claiming to count to row 410 while carrying a number that counted to 400;
and `verify.compare` has no vocabulary for a changed data validation, so allowing one
would weaken the gate for every future run. Those five items are printed by `efe check`,
`efe promote` and `efe fixup` from one function (`ranges.render_sheets_handoff`) and are
fixed by hand in Sheets.
