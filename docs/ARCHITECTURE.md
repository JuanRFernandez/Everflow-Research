# EverFlow Research Engine — Architecture

Version 1.0 · 2026-08-21
Owner: Juan Rodrigo Fernández · Repo: `Everflow-Research`

This document defines how the research tooling grows from a one-off contact
enricher into a durable, incremental market-research engine — without becoming a
data-hoarding machine that never produces a signed partner.

Read this before adding any new capability.

---

## 0. The governing principle

**The engine exists to keep the top of the funnel full while a human works the
bottom.** It is not a database competition.

The number that matters is not rows collected. It is:

```
discovered → enriched → prioritised → CONTACTED → replied → meeting → agreement
```

Every feature must be justified by its effect on the right-hand side of that
chain. A feature that only grows the left-hand side is a liability.

**Hard rule:** no new discovery round starts until every Priority-5 row from the
previous round has been contacted. This is enforced in code (`efe round start`
refuses to run and prints the outstanding rows), not by willpower.

---

## 1. Three concerns, currently tangled

The v1 tool does one of these. Keep them separate as the others arrive — they
have different failure modes, different cadences and different cost profiles.

| Concern | Question it answers | Cadence | Cost |
|---|---|---|---|
| **Discovery** | Who else exists that we don't know about? | Per round, deliberate | High (broad crawling) |
| **Enrichment** | For entities we know, what are the missing fields? | Continuous, background | Medium |
| **Scoring** | Of everything we know, who do we contact next? | Every export | Free (pure computation) |

Never let a discovery run silently trigger enrichment on 400 new rows. Discovery
produces *candidates*; a human promotes candidates to *entities*.

---

## 2. Data model — the single most important decision

### The problem

An `.xlsx` is a bad system of record for incremental work: no queries, no
history, no concurrency, no dedup, and openpyxl rewrites destroy formulas if
you're careless. But Excel is how the business actually operates — it is where
Juan works, where Cowork operates, and what lives in Google Drive.

### The resolution

**SQLite is the system of record. Excel is a generated artifact and a CRM
input.**

```
                 ┌──────────────┐
   discovery ───▶│              │
   enrichment ──▶│  research.db │──── export ───▶  Alpine_Partner_Database_vNN.xlsx
                 │   (SQLite)   │                        (Google Drive)
                 │              │◀─── import ────  CRM columns only
                 └──────────────┘
```

`research.db` lives at `data/research.db` in the repo and is **gitignored** — it
contains GDPR personal data. Back it up to Drive as a file copy, not as the live
working path (SQLite over a sync daemon corrupts).

### Ownership rule — this prevents merge hell

Bidirectional sync is where projects like this die. Avoid it with a strict
column-ownership split:

| Owned by SQLite (machine writes, export overwrites Excel) | Owned by Excel (human writes, import reads back) |
|---|---|
| Entity_Name, Category, Website_URL, emails, phones, contact persons, socials, segment, commission terms, source URLs, all scores | Contacted, Contact_Date, Follow_Up_Days, Email_Sent, Call_Made, WhatsApp_Sent, Meeting_Booked, Agreement_Signed, Status, Next_Action |

`import` reads **only** the right-hand column set. `export` writes **only** the
left-hand set, preserving whatever the import last captured. No field is ever
written by both sides. No conflict resolution is needed because no conflict is
possible.

`Next_Follow_Up` stays a live Excel formula and is never touched by either side.

### Core schema

```sql
entity(id, name, name_normalised, primary_domain, category, subcategory,
       resort_base, country, segment_tier, owner_group, created_round, status)

entity_field(entity_id, field, value, confidence, source_url, fetched_at, round_id)
  -- append-only. The current value of a field is the highest-confidence,
  -- most-recent row. This gives you full provenance and free history.

crm_state(entity_id, contacted, contact_date, status, next_action, updated_at)
  -- mirrors the human-owned columns

source_ledger(domain, round_id, purpose, outcome, entities_found, visited_at)

round(id, name, scope, started_at, closed_at, budget_requests, notes)

outcome(entity_id, event, event_date, notes)
  -- contacted / replied / meeting / proposal / agreement / declined
```

`entity_field` being append-only is what makes the whole thing auditable. You can
always answer "where did this email come from and when did we get it?" — which
matters both for GDPR and for trusting the data before an outreach campaign.

---

## 3. Discovery — the source plugin pattern

This is what makes the research "genérico e incremental". Every research vertical
is a plugin implementing one interface:

```python
class Source(Protocol):
    name: str
    category: str            # which PARTNERS category it feeds
    cost_estimate: int       # approx. HTTP requests per run

    def discover(self, scope: Scope) -> Iterable[Candidate]: ...
```

`Scope` carries the constraint set: countries, resort list, tier filter, max
results. `Candidate` is deliberately thin — name, domain, category, source_url,
raw evidence. Nothing more. Enrichment fills the rest.

Adding a vertical = adding one file in `src/sources/`. No changes elsewhere.

### Source backlog, in build order

Ordered by value-per-effort, not by interest:

1. **`resort_directory`** — scrape the accommodation/partner directories that
   every resort tourism board publishes. Highest yield, cleanest data, one
   pattern reuseable across 85 resorts.
2. **`google_places`** — hotels, rental shops, restaurants near a resort
   centroid. Best source for phone numbers and addresses at volume.
3. **`association_members`** — Dolomiti Superski operators, Relais & Châteaux,
   DSLV Profischulen, national ski-school associations. Pre-qualified lists.
4. **`chalet_platform`** — Le Collectionist, Firefly, Cimalpes listings →
   surfaces the independent chalet managers behind the platforms.
5. **`trade_exhibitors`** — ILTM, FESTURIS, Expo Ski SP exhibitor lists. This
   feeds the distribution category, which is where revenue actually comes from.
6. **`lift_company`** — B2B/group pages of the lift operators.
7. **`linkedin_company`** — named contacts and roles. Handle with care and rate
   limits; read the ToS before writing a line of it.

### Entity resolution — do not skip this

Discovery will re-find things you already have. Dedup on, in order:

1. `primary_domain` (strongest — a domain is nearly a primary key)
2. `name_normalised` + country
3. Nothing else

**Never auto-merge across differing domains.** Flag as `review_needed` and let a
human decide. A wrong merge silently destroys two records; a missed merge just
creates a duplicate you catch on export.

---

## 4. Scoring — Partner Priority Score (PPS)

`Priority_Score` is currently hand-typed. Compute it instead, so it re-ranks
automatically as data improves. Same philosophy as the resort SBI, applied to
partners:

```
PPS = w1·resort_SBI(base)        # is their resort worth being in at all
    + w2·segment_tier            # ultra-luxury > luxury > premium
    + w3·category_weight         # chalets/hotels/distribution > catering
    + w4·reachability            # do we have a named human?
    + w5·b2b_program_exists      # published trade programme = warm door
    + w6·climate_resilience      # their resort's exposure flag
    − w7·competitive_saturation  # how many schools already serve them
```

Weights live in `config.yml`, editable without touching code, exactly like the
SBI weights row in the workbook. Document the rationale for each weight change
in the round notes — a weight you can't justify is a bias you can't see.

**Design intent:** PPS answers "who do I email on Monday morning?" If it doesn't
change your Monday, the scoring is decorative and should be deleted.

---

## 5. Rounds — the project-management layer

Work happens in **rounds**, not continuously. A round is a declared scope, a
source set and a request budget.

```
efe round start --name "R2-chalets-CH" \
                --scope "country=CH,tier=T1+T2" \
                --sources chalet_platform,resort_directory \
                --budget 800
```

Round discipline gives you four things a continuous crawler cannot:

- **No re-treading.** `source_ledger` is checked before every fetch. Round 1
  already burned 223 domains; they are excluded by default.
- **Cost control.** A budget that halts the run beats discovering you made 40,000
  requests to a future partner's website.
- **Attribution.** `created_round` on every entity lets you ask which rounds
  actually produced signed partners — and stop running the ones that don't.
- **A natural stopping point.** Rounds end. Crawlers don't.

### Weekly cadence

| Day | Action |
|---|---|
| Mon | `efe export` → review top 20 by PPS → send outreach → log in Excel |
| Tue–Thu | Enrichment runs in the background against the existing backlog |
| Fri | `efe import` (pull CRM edits) → `efe report` → decide next round scope |
| Round close | Write round notes: what was found, what converted, what to change |

---

## 6. Measure what closes, not what accumulates

Instrument the funnel from day one. `efe report` outputs, per category:

- entities discovered / enriched / contacted
- reply rate, meeting rate, agreement rate
- median days from contact to reply
- cost per contacted entity (requests + time)

After roughly 50 outreaches you will know whether hotels, chalets or distribution
actually answer. **Then point discovery at what converts and stop researching what
doesn't.** That single feedback loop is worth more than every other feature in
this document.

Expect the answer to be uncomfortable. It is common to find that the category you
find most interesting converts worst.

---

## 7. Build order — do not build this all now

Good engineering practice is to refuse to build the framework before you have two
instances of the thing it abstracts.

| Phase | Build | Trigger to start |
|---|---|---|
| **0 — now** | The contact enricher, exactly as specced. One workbook in, one out. | — |
| **1** | SQLite store + `export`/`import` with the ownership split. | Phase 0 works end-to-end on all 253 rows |
| **2** | The `Source` interface + the **two** highest-value plugins. | You have run Phase 1 for at least one full week |
| **3** | PPS scoring + `outcome` tracking + `efe report`. | ≥50 outreaches logged, so there's data to score against |
| **4** | Additional source plugins, one at a time. | A specific category proves it converts |

Phase 2 is where most projects over-engineer. Two plugins is enough to prove the
interface. If you write the third before shipping the first two, stop.

### Explicitly out of scope

Listed so nobody proposes them in a weak moment:

- A web UI. Excel is the UI.
- A CRM. The Excel CRM columns are the CRM until volume breaks them.
- Automated outreach or mail-merge sending. Outreach stays human — the entire
  moat is that a real person with a DSLV licence and 60 FIS starts is writing.
- Multi-user concurrency. One operator, one machine.
- Any LLM-based extraction where a CSS selector works. Deterministic beats clever.

---

## 8. Standing engineering rules

- **Never fabricate a contact detail.** An invented email burns the sending
  domain on the first campaign. `TBD` is a valid, honest answer.
- **Provenance on every written field.** Source URL and fetch timestamp, always.
- **Respect robots.txt and rate limits.** Several of these targets are future
  partners. Getting blocked is a commercial problem, not a technical one.
- **Cache every fetch.** Reruns must be free and auditable.
- **Resumable by default.** Any run that dies mid-way restarts where it stopped.
- **Never write over the input.** New version file, always, following
  `YYYY-MM-DD_EFE_Name_vNN.xlsx`.
- **`*.xlsx` and `data/` stay gitignored.** The workbook holds GDPR personal
  data. It must never enter version history.
- **Fixtures, not network, in tests.** The test suite runs offline.

---

## 9. Where the data lives

```
C:\Users\rofer\GIT_repositories\Everflow_Experience\     code (Git)
  ├── src/                                               engine
  ├── tests/                                             offline fixtures
  ├── config.yml                                         paths, weights, limits
  └── data/                                              gitignored
      ├── research.db                                    system of record
      └── cache/                                         fetched pages

G:\My Drive\...\05_PARTNERS_AGENCIES_B2B\                data (Drive, offline-synced)
  └── 2026-08-21_EFE_Alpine_Partner_Database_vNN.xlsx    the working artifact
```

Git versions the code. Drive versions the data. Neither is ever nested inside the
other: a `.git` directory under a sync daemon corrupts, and a workbook inside the
repo leaks personal data into history permanently.
