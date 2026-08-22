"""Run reporting: the console dry-run view, the Markdown report and the review CSV.

The dry-run view exists to make the tool's judgement inspectable. For every address
it shows which column it would go to and the exact reason -- which token matched,
what page it came from, and why anything held back was held back.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from efe.config import Config
from efe.models import CellChange, Confidence, RunSummary, ScopeVerdict
from efe.pipeline import FIELD_TO_COLUMN_KEY, RunOutcome
from efe.workbook.reader import ENRICHABLE_FIELDS, WorkbookView

REVIEW_CSV_HEADERS = [
    "Row", "Entity_ID", "Entity_Name", "Would_Go_To", "Column", "Value",
    "Confidence", "Data_Class", "Held_Back_Because", "Source_URL", "Fetched_At",
    "Extractor",
]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary(
    cfg: Config,
    view: WorkbookView,
    outcome: RunOutcome,
    *,
    run_id: str,
    round_id: str,
    started_at: datetime,
    dry_run: bool,
    selected: int,
    skipped: dict[int, str],
    workbook_out: str = "",
) -> RunSummary:
    spec = cfg.workbook
    still_tbd: Counter[str] = Counter()
    for logical in ENRICHABLE_FIELDS:
        column = spec.column_for(logical)
        filled_here = {c.row for c in outcome.changes if c.column == column}
        for result in outcome.results:
            if result.candidate.row in filled_here:
                continue
            if spec.is_empty(result.candidate.existing.get(logical, "")):
                still_tbd[f"{column} {logical}"] += 1

    failures = [
        {"row": r.candidate.row, "entity": r.candidate.name, "errors": "; ".join(r.errors)}
        for r in outcome.results
        if r.errors
    ]

    duplicates = [
        {
            "domain": domain,
            "rows": rows,
            "entities": [
                pr.get(spec.column_for("entity_name"))
                for pr in view.rows
                if pr.row in rows
            ],
        }
        for domain, rows in sorted(view.duplicate_domains.items())
    ]

    shared_rows = [
        {
            "row": r.candidate.row,
            "entity": r.candidate.name,
            "domain": r.candidate.domain,
            "why_shared": r.shared_domain_reason,
            "needed_tokens": r.required_tokens,
            # A row whose pages all failed never reached the guard at all;
            # reporting its default verdict would claim a judgement never made.
            "verdict": (
                "no pages fetched" if not r.pages_fetched
                else r.scope_verdict.value
            ),
            "pages_matched": r.pages_matched,
            "pages_unmatched": r.pages_unmatched,
            "cells_written": sum(1 for c in outcome.changes if c.row == r.candidate.row),
            "held": sum(1 for c in outcome.held if c.row == r.candidate.row),
        }
        for r in outcome.results
        if r.shared_domain
    ]

    name_col = spec.column_for("entity_name")
    web_col = spec.column_for("website_url")
    manual_rows = [
        {
            "row": str(pr.row),
            "entity": pr.get(name_col),
            "website": pr.get(web_col),
        }
        for pr in view.rows
        if skipped.get(pr.row, "").startswith("needs_manual_url")
    ]

    return RunSummary(
        run_id=run_id,
        round_id=round_id,
        started_at=started_at,
        finished_at=datetime.now(),
        dry_run=dry_run,
        workbook_in=str(view.path),
        workbook_out=workbook_out,
        rows_total=len(view.rows),
        rows_selected=selected,
        rows_processed=len(outcome.results),
        rows_skipped=dict(Counter(skipped.values())),
        cells_written=dict(Counter(f"{c.column} {c.field}" for c in outcome.changes)),
        cells_still_tbd=dict(still_tbd),
        held_for_review=len(outcome.held),
        alternates_dropped=outcome.alternates_dropped,
        domains_abandoned=outcome.domains_abandoned,
        pages_fetched=outcome.pages_fetched,
        cache_hits=outcome.cache_hits,
        failures=failures,
        robots_blocked=sorted(set(outcome.robots_blocked)),
        revisited_domains=outcome.revisited,
        duplicate_entities=duplicates,
        shared_domain_rows=shared_rows,
        needs_manual_url_rows=manual_rows,
    )


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

def _scope_label(verdict: ScopeVerdict) -> str:
    return {
        ScopeVerdict.OWN_DOMAIN: "[green]own domain[/green]",
        ScopeVerdict.SHARED_MATCHED: "[yellow]group domain, matched[/yellow]",
        ScopeVerdict.SHARED_UNMATCHED: "[red]group domain, NOT matched[/red]",
    }[verdict]


def _confidence_label(confidence: Confidence) -> str:
    return {
        Confidence.HIGH: "[green]high[/green]",
        Confidence.MEDIUM: "[yellow]medium[/yellow]",
        Confidence.LOW: "[red]low[/red]",
    }[confidence]


def print_dry_run(console: Console, cfg: Config, outcome: RunOutcome) -> None:
    """Per-row detail: what was fetched, what was found, and every routing decision."""
    changes_by_row: dict[int, list[CellChange]] = {}
    held_by_row: dict[int, list[CellChange]] = {}
    for change in outcome.changes:
        changes_by_row.setdefault(change.row, []).append(change)
    for change in outcome.held:
        held_by_row.setdefault(change.row, []).append(change)

    email_columns = {
        cfg.workbook.column_for("general_email"),
        cfg.workbook.column_for("sales_b2b_email"),
    }

    for result in outcome.results:
        candidate = result.candidate
        console.rule(
            f"[bold]row {candidate.row}[/bold]  {candidate.entity_id}  "
            f"{candidate.name}",
            align="left",
        )
        console.print(
            f"  domain      {candidate.domain}   "
            f"scope: {_scope_label(result.scope_verdict)}"
        )
        console.print(f"  pages       {len(result.pages_fetched)} fetched")
        for url in result.pages_fetched:
            console.print(f"                [dim]{url}[/dim]")
        if result.revisited_ledger_domain:
            console.print(
                "  [dim]note        domain is in the Round-1 _SOURCES ledger; "
                "revisited for contact extraction[/dim]"
            )
        for error in result.errors:
            console.print(f"  [red]error       {error}[/red]")

        writes = changes_by_row.get(candidate.row, [])
        if writes:
            table = Table(
                "col", "field", "value", "conf", "why", show_lines=False,
                title="WOULD WRITE", title_justify="left", title_style="bold green",
                box=None, pad_edge=False,
            )
            for change in sorted(writes, key=lambda c: c.column):
                table.add_row(
                    change.column,
                    change.field,
                    change.new_value[:60],
                    _confidence_label(change.confidence),
                    change.note[:150],
                )
            console.print(table)
        else:
            console.print("  [dim]would write nothing[/dim]")

        held = held_by_row.get(candidate.row, [])
        if held:
            table = Table(
                "would be", "value", "conf", "held back because", box=None,
                title="HELD FOR REVIEW", title_justify="left", title_style="bold yellow",
                pad_edge=False,
            )
            for change in sorted(held, key=lambda c: c.column)[:12]:
                marker = "  <- email routing" if change.column in email_columns else ""
                table.add_row(
                    f"{change.column} {change.field}{marker}",
                    change.new_value[:50],
                    _confidence_label(change.confidence),
                    change.note.replace("HELD FOR REVIEW - ", "")[:150],
                )
            console.print(table)
            if len(held) > 12:
                console.print(f"  [dim]... and {len(held) - 12} more in the review queue[/dim]")
        console.print()


def print_summary(console: Console, summary: RunSummary) -> None:
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_row("run", f"{summary.run_id}  (round {summary.round_id})")
    table.add_row("mode", "[yellow]DRY RUN - nothing written[/yellow]"
                  if summary.dry_run else "[green]WRITE[/green]")
    table.add_row("rows", f"{summary.rows_processed} processed of "
                          f"{summary.rows_selected} selected / {summary.rows_total} total")
    table.add_row("pages", f"{summary.pages_fetched} requests, "
                           f"{summary.cache_hits} served from cache")
    table.add_row("cells filled", str(sum(summary.cells_written.values())))
    table.add_row("held for review", str(summary.held_for_review))
    if summary.alternates_dropped:
        table.add_row(
            "alternates dropped",
            f"{summary.alternates_dropped} beyond the per-field cap "
            "(see the run report)",
        )
    table.add_row("failures", str(len(summary.failures)))
    if summary.domains_abandoned:
        table.add_row("domains abandoned", str(len(summary.domains_abandoned)))
    if summary.workbook_out:
        table.add_row("written to", summary.workbook_out)
    console.print(table)

    if summary.cells_written:
        breakdown = Table("column", "filled", box=None, pad_edge=False,
                          title="cells filled by column", title_justify="left")
        for key, count in sorted(summary.cells_written.items()):
            breakdown.add_row(key, str(count))
        console.print(breakdown)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def render_markdown(summary: RunSummary, outcome: RunOutcome) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# EFE enrichment run `{summary.run_id}`")
    add("")
    add(f"- **Round**: `{summary.round_id}`")
    add(f"- **Mode**: {'dry run - nothing written' if summary.dry_run else 'write'}")
    add(f"- **Started**: {summary.started_at.isoformat(timespec='seconds')}")
    finished = (
        summary.finished_at.isoformat(timespec="seconds") if summary.finished_at else "-"
    )
    add(f"- **Finished**: {finished}")
    add(f"- **Input**: `{summary.workbook_in}`")
    if summary.workbook_out:
        add(f"- **Output**: `{summary.workbook_out}`")
    add("")

    add("## Totals")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Rows in PARTNERS | {summary.rows_total} |")
    add(f"| Rows selected for enrichment | {summary.rows_selected} |")
    add(f"| Rows processed this run | {summary.rows_processed} |")
    add(f"| Cells filled | {sum(summary.cells_written.values())} |")
    add(f"| Candidates held for review | {summary.held_for_review} |")
    add(f"| Alternates dropped beyond the per-field cap | {summary.alternates_dropped} |")
    add(f"| HTTP requests made | {summary.pages_fetched} |")
    add(f"| Pages served from cache | {summary.cache_hits} |")
    add(f"| Entities with errors | {len(summary.failures)} |")
    add("")

    add("## Rows skipped, with reasons")
    add("")
    add("| Reason | Rows |")
    add("|---|---|")
    for reason, count in sorted(summary.rows_skipped.items(), key=lambda kv: -kv[1]):
        add(f"| {reason} | {count} |")
    add("")

    add("## Cells filled by column")
    add("")
    if summary.cells_written:
        add("| Column | Field | Filled |")
        add("|---|---|---|")
        for key, count in sorted(summary.cells_written.items()):
            column, _, fieldname = key.partition(" ")
            add(f"| {column} | {fieldname} | {count} |")
    else:
        add("_Nothing was filled._")
    add("")

    add("## Still TBD after this run")
    add("")
    add("| Column | Field | Still TBD |")
    add("|---|---|---|")
    for key, count in sorted(summary.cells_still_tbd.items()):
        column, _, fieldname = key.partition(" ")
        add(f"| {column} | {fieldname} | {count} |")
    add("")

    add("## Failures")
    add("")
    if summary.failures:
        add("| Row | Entity | Error |")
        add("|---|---|---|")
        for failure in summary.failures:
            add(f"| {failure['row']} | {failure['entity']} | {failure['errors']} |")
    else:
        add("_None._")
    add("")

    add("## Domains abandoned mid-run")
    add("")
    add(
        "A domain that answers several requests in a row with an error is telling us "
        "to stop. The rest of its page plan is skipped rather than walked; these are "
        "future partners, not targets."
    )
    add("")
    if summary.domains_abandoned:
        add("| Domain | Reason |")
        add("|---|---|")
        for domain, reason in sorted(summary.domains_abandoned.items()):
            add(f"| {domain} | {reason} |")
    else:
        add("_None._")
    add("")

    add("## Review-queue cap")
    add("")
    if summary.alternates_dropped:
        add(
            f"The review queue keeps the best-evidenced alternates per row per field. "
            f"**{summary.alternates_dropped} further candidates were found and dropped "
            f"beyond that cap.** Raise `review.max_alternates_per_field` in "
            f"`config.yaml` to see more of them."
        )
    else:
        add("_Every candidate found is in the review queue; nothing was dropped._")
    add("")

    add("## robots.txt-blocked URLs")
    add("")
    if summary.robots_blocked:
        for url in summary.robots_blocked:
            add(f"- `{url}`")
    else:
        add("_None. No disallowed URL was fetched._")
    add("")

    add("## Round-1 ledger domains revisited")
    add("")
    add(
        "`_SOURCES` flags 223 domains `Exclude_Next_Round = YES`. That flag governs "
        "*discovery*; enrichment revisits them deliberately, because Round 1 visited "
        "them to find entities rather than to extract contacts -- which is exactly why "
        "those rows still read TBD. Every revisit is listed here."
    )
    add("")
    if summary.revisited_domains:
        add("| Domain | Entity | Round-1 purpose | Reason for revisiting |")
        add("|---|---|---|---|")
        for entry in summary.revisited_domains:
            add(
                f"| {entry['domain']} | {entry['entity']} | "
                f"{entry.get('round_1_purpose', '')} | {entry['reason']} |"
            )
    else:
        add("_None of the domains visited this run appear in the Round-1 ledger._")
    add("")

    add("## Rows needing a manual URL")
    add("")
    add(
        "These domains refuse automated access -- 403 to every request, or a TLS "
        "stack too old to negotiate. There is no polite workaround, and an impolite "
        "one is not on the table: these are future partners. The rows were **not "
        "fetched at all**, so no request was wasted on them."
    )
    add("")
    add(
        "Replace each row's `Website_URL` with the property's own page and it "
        "enriches normally on the next run. The domain list is "
        "`scope.needs_manual_url` in `config.yaml`."
    )
    add("")
    if summary.needs_manual_url_rows:
        add("| Row | Entity | Current Website_URL |")
        add("|---|---|---|")
        for entry in summary.needs_manual_url_rows:
            add(f"| {entry['row']} | {entry['entity']} | `{entry['website']}` |")
    else:
        add("_None._")
    add("")

    add("## Shared-domain guard: which rows it applied to")
    add("")
    add(
        "A domain used by more than one PARTNERS row, or listed in "
        "`scope.chain_domains`, is a group site. On those rows a value is only "
        "written when the page it came from names that specific property in its URL "
        "path, title or headings, and the group header, navigation and footer are "
        "stripped before contact extraction. `shared_unmatched` means no fetched page "
        "identified the property, so **nothing was written** and everything found is "
        "in the review queue."
    )
    add("")
    if summary.shared_domain_rows:
        add(
            "| Row | Entity | Domain | Why shared | Needed | Pages named it "
            "| Verdict | Written | Held |"
        )
        add("|---|---|---|---|---|---|---|---|---|")
        for entry in sorted(summary.shared_domain_rows, key=lambda e: e["row"]):
            tokens = ", ".join(entry["needed_tokens"]) or "_(row is the group itself)_"
            total_pages = entry["pages_matched"] + entry["pages_unmatched"]
            named = (
                f"{entry['pages_matched']}/{total_pages}" if total_pages else "—"
            )
            add(
                f"| {entry['row']} | {entry['entity']} | `{entry['domain']}` | "
                f"{entry['why_shared']} | {tokens} | {named} "
                f"| `{entry['verdict']}` | {entry['cells_written']} | {entry['held']} |"
            )
    else:
        add("_No row in this run sat on a shared or chain domain._")
    add("")

    add("## Duplicate / shared domains (for Phase-1 dedup)")
    add("")
    if summary.duplicate_entities:
        add("| Domain | Rows | Entities |")
        add("|---|---|---|")
        for entry in summary.duplicate_entities:
            entities = ", ".join(str(e) for e in entry["entities"])
            add(f"| {entry['domain']} | {entry['rows']} | {entities} |")
    else:
        add("_None._")
    add("")

    scope_blocked = [
        c for c in outcome.held if "group/chain domain" in c.note
    ]
    add("## Rows needing a property-level Website_URL")
    add("")
    if scope_blocked:
        add(
            "These rows point at a group or chain domain and no fetched page named "
            "the specific property, so nothing was written for them. Replacing "
            "`Website_URL` with the property's own page would unblock them."
        )
        add("")
        add("| Row | Entity |")
        add("|---|---|")
        for row, entity in sorted({(c.row, c.entity_name) for c in scope_blocked}):
            add(f"| {row} | {entity} |")
    else:
        add("_None._")
    add("")
    return "\n".join(lines)


WOULD_WRITE_CSV_HEADERS = [
    "Row", "Entity_ID", "Entity_Name", "Column", "Field", "Old_Value", "New_Value",
    "Confidence", "Data_Class", "Source_URL", "Fetched_At", "Extractor", "Why",
]


def render_changes_csv(changes: list[CellChange]) -> str:
    """Every cell that would be, or was, written -- with the reason for each."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(WOULD_WRITE_CSV_HEADERS)
    for change in sorted(changes, key=lambda c: (c.row, c.column)):
        writer.writerow(
            [
                change.row, change.entity_id, change.entity_name, change.column,
                change.field, change.old_value, change.new_value,
                change.confidence.value, change.data_class.value, change.source_url,
                change.fetched_at.isoformat(timespec="seconds"), change.extractor,
                change.note,
            ]
        )
    return buffer.getvalue()


def _md_cell(text: str, limit: int = 300) -> str:
    """Escape a value for a markdown table cell."""
    clean = " ".join(str(text or "").split()).replace("|", "\\|")
    return clean[:limit] + ("..." if len(clean) > limit else "") or "&nbsp;"


def render_decision_table(
    summary: RunSummary, outcome: RunOutcome
) -> str:
    """One readable table row per decision, written and held alike."""
    lines: list[str] = []
    add = lines.append
    mode = "dry run - nothing was written" if summary.dry_run else "write"

    add(f"# EFE enrichment - decisions, run `{summary.run_id}`")
    add("")
    add(f"**Mode:** {mode}  ")
    add(f"**Round:** `{summary.round_id}`  ")
    add(f"**Workbook:** `{summary.workbook_in}`  ")
    add(f"**Rows processed:** {summary.rows_processed}  ")
    add(
        f"**Cells filled:** {sum(summary.cells_written.values())}  ·  "
        f"**Held for review:** {summary.held_for_review}  ·  "
        f"**Alternates dropped beyond the cap:** {summary.alternates_dropped}"
    )
    add("")

    add("## Would be written" if summary.dry_run else "## Written")
    add("")
    if outcome.changes:
        add("| Entity | Field | Old | New | Confidence | Source URL | Decision reason |")
        add("|---|---|---|---|---|---|---|")
        for change in sorted(outcome.changes, key=lambda c: (c.row, c.column)):
            add(
                f"| {_md_cell(change.entity_name, 44)} "
                f"| {change.column} {_md_cell(change.field, 24)} "
                f"| {_md_cell(change.old_value, 24)} "
                f"| {_md_cell(change.new_value, 60)} "
                f"| {change.confidence.value} "
                f"| {_md_cell(change.source_url, 90)} "
                f"| {_md_cell(change.note, 260)} |"
            )
    else:
        add("_Nothing met the write threshold._")
    add("")

    add("## Held for review - found, published, but not written")
    add("")
    add(
        "Every candidate below was genuinely on a page that was fetched. It is here "
        "rather than in the workbook because of the reason in the last column."
    )
    add("")
    if outcome.held:
        add("| Entity | Would go to | Value | Confidence | Source URL | Held back because |")
        add("|---|---|---|---|---|---|")
        for change in sorted(outcome.held, key=lambda c: (c.row, c.column, c.new_value)):
            reason = change.note.replace("HELD FOR REVIEW - ", "")
            add(
                f"| {_md_cell(change.entity_name, 44)} "
                f"| {change.column} {_md_cell(change.field, 24)} "
                f"| {_md_cell(change.new_value, 56)} "
                f"| {change.confidence.value} "
                f"| {_md_cell(change.source_url, 90)} "
                f"| {_md_cell(reason, 260)} |"
            )
    else:
        add("_Nothing was held back._")
    add("")
    return "\n".join(lines)


def render_review_csv(held: list[CellChange]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REVIEW_CSV_HEADERS)
    for change in sorted(held, key=lambda c: (c.row, c.column, c.new_value)):
        writer.writerow(
            [
                change.row,
                change.entity_id,
                change.entity_name,
                change.field,
                change.column,
                change.new_value,
                change.confidence.value,
                change.data_class.value,
                change.note.replace("HELD FOR REVIEW - ", ""),
                change.source_url,
                change.fetched_at.isoformat(timespec="seconds"),
                change.extractor,
            ]
        )
    return buffer.getvalue()


def write_outputs(
    directory: Path,
    stem: str,
    summary: RunSummary,
    outcome: RunOutcome,
) -> dict[str, Path]:
    """Write every process artifact for a run into one local directory.

    The workbook is emitted separately by `workbook.writer`; nothing here ever goes
    to the Drive folder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    table = directory / f"dry_run_{summary.run_id}.md"
    table.write_text(render_decision_table(summary, outcome), encoding="utf-8")
    written["decisions"] = table

    report = directory / f"{stem}_run_report.md"
    report.write_text(render_markdown(summary, outcome), encoding="utf-8")
    written["report"] = report

    payload = directory / f"{stem}_run_report.json"
    payload.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    written["json"] = payload

    review = directory / f"{stem}_review_queue.csv"
    review.write_text(render_review_csv(outcome.held), encoding="utf-8-sig")
    written["review"] = review

    changes = directory / f"{stem}_changes.csv"
    changes.write_text(render_changes_csv(outcome.changes), encoding="utf-8-sig")
    written["changes"] = changes

    return written


__all__ = [
    "FIELD_TO_COLUMN_KEY",
    "build_summary",
    "render_changes_csv",
    "render_decision_table",
    "print_dry_run",
    "print_summary",
    "render_markdown",
    "render_review_csv",
    "write_outputs",
]
