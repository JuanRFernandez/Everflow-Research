"""Command line entry point.

`efe <verb>`. Phase 0 ships `enrich`, `check`, `verify` and `duplicates`; the
Phase-1+ verbs (`export`, `import`, `round start`, `report`) slot in beside them
without restructuring anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from efe import __version__
from efe import config as config_mod
from efe.dedupe import find_duplicates, render_duplicates_report
from efe.models import (
    DriveSyncError,
    SchemaMismatchError,
    VerificationError,
    WorkbookGuardError,
    WorkbookLockedError,
    today_iso,
)
from efe.pipeline import RunLedger, run_enrichment
from efe.report import build_summary, print_dry_run, print_summary, write_outputs
from efe.workbook.fixup import (
    assert_fixups_legal,
    assert_ids_unique_after,
    propose_fixups,
    read_fixup_plan,
    write_fixup_plan,
    write_fixups,
)
from efe.workbook.promote import plan_promotion, read_candidates, write_promoted
from efe.workbook.ranges import inspect_ranges, inspect_sheet_ranges, render_sheets_handoff
from efe.workbook.reader import (
    WorkbookView,
    file_fingerprint,
    load_workbook_view,
    record_input_state,
    resolve_input,
    select_candidates,
)
from efe.workbook.resolve import Resolution
from efe.workbook.state import WorkbookState, save_state, state_path
from efe.workbook.verify import compare, format_report, snapshot
from efe.workbook.writer import next_version_path, write_enriched

console = Console()


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _add_workbook_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--workbook",
        type=Path,
        default=None,
        help="read this file instead of the highest version in workbook_dir",
    )
    sub.add_argument(
        "--reset-state",
        action="store_true",
        help=(
            "accept the chosen workbook as the new baseline even if it has fewer "
            "rows or a lower version than the last run recorded"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="efe",
        description=(
            "EverFlow Experience research engine. Phase 0: fill missing partner "
            "contact fields from each company's own website."
        ),
    )
    parser.add_argument("--version", action="version", version=f"efe {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enrich = subparsers.add_parser(
        "enrich", help="fetch company sites and fill missing contact fields"
    )
    enrich.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD change; write no workbook",
    )
    enrich.add_argument("--limit", type=int, default=None, help="process at most N rows")
    enrich.add_argument(
        "--rows",
        default=None,
        metavar="A:B",
        help="restrict to worksheet rows A to B inclusive, e.g. 2:21",
    )
    _add_workbook_args(enrich)
    enrich.add_argument("--round", dest="round_id", default=None, help="round id, e.g. R2")
    enrich.add_argument("--no-cache", action="store_true", help="refetch, ignore the page cache")
    enrich.add_argument(
        "--fresh", action="store_true", help="ignore the resume ledger and start over"
    )
    enrich.add_argument(
        "--categories",
        default=None,
        metavar="1,2,3|all",
        help="override selection.categories for this run; 'all' clears the filter",
    )
    enrich.add_argument(
        "--resorts",
        default=None,
        metavar="A,B|all",
        help="override selection.resorts for this run; 'all' clears the filter",
    )

    check = subparsers.add_parser(
        "check",
        help="resolve the workbook, validate the contract, report what was found",
    )
    _add_workbook_args(check)

    duplicates = subparsers.add_parser(
        "duplicates",
        help="report duplicate PARTNERS rows (accent-insensitive); changes nothing",
    )
    _add_workbook_args(duplicates)

    verify = subparsers.add_parser(
        "verify", help="prove an emitted workbook is faithful to its input"
    )
    verify.add_argument("output", type=Path, help="the emitted file to check")
    verify.add_argument("--against", type=Path, required=True, help="the input it came from")
    promote = subparsers.add_parser(
        "promote",
        help="append candidate rows from a PARTNERS-shaped CSV as a new workbook version",
    )
    promote.add_argument("candidates", type=Path, help="CSV with PARTNERS columns")
    promote.add_argument("--dry-run", action="store_true", help="print the plan; write no workbook")
    _add_workbook_args(promote)

    fixup = subparsers.add_parser(
        "fixup",
        help="apply a reviewed plan of corrections (renumber IDs, normalise cells)",
    )
    fixup.add_argument("plan", type=Path, nargs="?", default=None, help="the plan CSV to apply")
    fixup.add_argument(
        "--propose",
        action="store_true",
        help="read the workbook and write a draft plan CSV for review; change nothing",
    )
    fixup.add_argument("--dry-run", action="store_true", help="print the plan; write no workbook")
    fixup.add_argument(
        "--reserve",
        type=Path,
        action="append",
        default=None,
        metavar="CSV",
        help="a PARTNERS-shaped CSV whose IDs are spoken for (repeatable)",
    )
    fixup.add_argument(
        "--max-cells", type=int, default=None, help="ceiling on cell edits in one plan"
    )
    fixup.add_argument(
        "--allow-backfill",
        action="store_true",
        help="let a renumber reuse a number below the sheet's highest ID",
    )
    fixup.add_argument(
        "--skip-stale",
        action="store_true",
        help="apply the records that still match instead of refusing the whole plan",
    )
    _add_workbook_args(fixup)

    verify.add_argument(
        "--allow-partners-changes",
        action="store_true",
        help="tolerate value changes on PARTNERS (they are expected after an enrich)",
    )
    verify.add_argument(
        "--expect-plan",
        type=Path,
        default=None,
        metavar="CSV",
        help="prove only the cells this fixup plan names changed on PARTNERS",
    )
    return parser


def parse_rows(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    try:
        start, _, end = spec.partition(":")
        return int(start), int(end or start)
    except ValueError as exc:
        raise SystemExit(f"--rows expects A:B with integers, got {spec!r}") from exc


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def print_resolution_block(cfg, res: Resolution | None) -> None:
    """Which file, and why -- printed BEFORE the file is opened, so a failure that
    follows (schema, continuity, lock) is never an anonymous one."""
    console.print("[bold]Workbook resolution[/bold]")
    if res is None:
        return
    console.print(f"  folder    {res.directory}")
    for extra in res.extra_directories:
        console.print(f"  [dim]also      {extra}[/dim]")
    chosen = res.chosen
    if chosen is not None:
        note = "  [dim](--workbook override)[/dim]" if res.override else ""
        version = f"v{chosen.version:02d}" if chosen.version else "unversioned"
        console.print(
            f"  chosen    [green]{chosen.path.name}[/green]  {version}  "
            f"{chosen.size:,} bytes  modified {chosen.mtime_iso}{note}"
        )
    for name, reason in res.rejected:
        console.print(f"  [dim]skipped   {name}  -- {reason}[/dim]")


def print_resolution_report(cfg, view: WorkbookView, *, reset_state: bool = False) -> None:
    """Everything `efe check` learned: which file, why, and what it looks like."""
    print_resolution_block(cfg, view.resolution)
    print_contract_report(cfg, view, reset_state=reset_state)


def print_contract_report(cfg, view: WorkbookView, *, reset_state: bool = False) -> None:
    """Contract, rows and continuity of a workbook that passed every gate, so each
    line reads OK; the failing cases carry their full detail (header diff, baseline)
    in the exception message instead."""
    spec = cfg.workbook
    schema = view.schema

    console.print("[bold]Contract[/bold]")
    console.print(
        f"  sheets    [green]OK[/green]  {len(spec.required_sheets)} required present; "
        f"found {view.sheets}"
    )
    console.print(f"  header    [green]OK[/green]  {len(view.header)} columns, exact and in order")
    if schema is not None:
        padded = f"; {schema.padded_rows} padded blank rows ignored" if schema.padded_rows else ""
        console.print(
            f"  rows      {view.data_rows} data rows "
            f"(worksheet rows {spec.first_data_row}..{schema.last_row}{padded})"
        )
    for name in spec.formula_columns:
        typed = schema.formula_overrides.get(name, []) if schema is not None else []
        if typed:
            console.print(
                f"  formula   [green]OK[/green]  {name} ({spec.letter_of(name)}) holds a "
                f"formula on {view.data_rows - len(typed)} data rows; "
                f"[yellow]{len(typed)} human-verified row(s) carry a typed value "
                f"instead[/yellow]: {typed[:12]}"
            )
        else:
            console.print(
                f"  formula   [green]OK[/green]  {name} ({spec.letter_of(name)}) holds a "
                f"formula on all {view.data_rows} data rows"
            )

    duplicates = schema.duplicate_ids if schema is not None else {}
    if duplicates:
        sample = ", ".join(
            f"{k} (rows {', '.join(str(r) for r in v)})" for k, v in list(duplicates.items())[:2]
        )
        console.print(
            f"  ids       [yellow]WARN[/yellow]  {view.data_rows} rows, "
            f"{view.data_rows - sum(len(v) - 1 for v in duplicates.values())} distinct IDs - "
            f"{len(duplicates)} used more than once, e.g. {sample}"
        )
    else:
        console.print(
            f"  ids       [green]OK[/green]  {view.data_rows} rows, {view.data_rows} distinct IDs"
        )

    writer = (
        f"last written by {view.last_writer}"
        if view.last_writer
        else "no docProps/app.xml, as in a Sheets export"
    )
    if view.formula_cells and view.cached_results:
        console.print(
            f"  cache     {view.cached_results} of {view.formula_cells} formula cells carry "
            f"a cached result  [dim]({writer})[/dim]"
        )
    elif view.formula_cells:
        console.print(
            f"  cache     [yellow]none[/yellow] of {view.formula_cells} formula cells carry a "
            f"cached result ({writer}). Sheets and "
            "Excel recompute on open; the output will carry none either."
        )
    ranges = inspect_sheet_ranges(view.path, cfg)
    drift = ranges.drift()
    if drift:
        console.print("[bold]Ranges[/bold]  [dim](Sheets-side, fixed by hand -- never fatal)[/dim]")
        for line in drift:
            console.print(f"  [yellow]{line}[/yellow]")

    console.print("[bold]Continuity[/bold]")
    prev = view.previous_state
    if prev is None:
        console.print("  baseline  none (first run) - this file is recorded as the baseline")
    else:
        console.print(
            f"  baseline  {prev.file}  {prev.version_label}  {prev.data_rows} rows  "
            f"[dim]({prev.recorded_at}, {prev.command})[/dim]"
        )
        if reset_state:
            console.print("  status    [yellow]reset[/yellow]  this file is the new baseline")
        elif view.continuity_notices:
            console.print("  status    [yellow]DRIFT[/yellow]")
            for notice in view.continuity_notices:
                console.print(f"      {notice}")
        else:
            console.print(
                f"  status    [green]OK[/green]  v{view.version:02d} with {view.data_rows} "
                "rows is not behind the baseline"
            )
    if view.fingerprint:
        console.print(f"  sha256    [dim]{view.fingerprint}[/dim]")
    console.print(f"  recorded  [dim]{state_path(cfg.state_directory)}[/dim]")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    # The report below prints the resolution in full; keep the log line out of it.
    # The report below prints resolution, duplicate IDs and drift in full.
    logging.getLogger("efe.workbook.resolve").setLevel(logging.ERROR)
    logging.getLogger("efe.workbook.reader").setLevel(logging.ERROR)
    chosen, resolution = resolve_input(cfg, args.workbook)
    print_resolution_block(cfg, resolution)
    view = load_workbook_view(
        cfg,
        args.workbook,
        reset_state=args.reset_state,
        command="efe check",
        resolved=(chosen, resolution),
    )
    candidates, skipped = select_candidates(view, cfg)

    print_contract_report(cfg, view, reset_state=args.reset_state)
    console.print("[bold]Selection[/bold]")
    targets = []
    if cfg.selection.categories:
        targets.append(f"categories={','.join(cfg.selection.categories)}")
    if cfg.selection.resorts:
        targets.append(f"resorts={', '.join(cfg.selection.resorts)}")
    console.print(
        "  targeting " + ("; ".join(targets) if targets else "none (every category, every resort)")
    )
    console.print(
        f"  {len(candidates)} rows selected for enrichment, {len(skipped)} skipped "
        f"({view.formula_count} formulas in the workbook)"
    )
    for reason in sorted(set(skipped.values())):
        count = sum(1 for r in skipped.values() if r == reason)
        console.print(f"  [dim]skip x{count}: {reason}[/dim]")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    before, after = snapshot(args.against), snapshot(args.output)
    allowed = set()
    if args.allow_partners_changes:
        sheet = cfg.workbook.sheet
        allowed = {key for key in set(before.values) | set(after.values) if key[0] == sheet}

    if args.expect_plan is not None:
        # Prove the narrow claim: only the cells this plan names changed on the data
        # sheet. `--allow-partners-changes` is far too coarse for a corrective release.
        plan = read_fixup_plan(args.expect_plan, cfg)
        spec = cfg.workbook
        spec.bind_header(spec.header)
        allowed = {(spec.sheet, f"{spec.letter_of(r.column)}{r.row}") for r in plan.records}
        console.print(
            f"[dim]{len(allowed)} cell(s) named by {args.expect_plan.name} (exempt)[/dim]"
        )

    new_sheets = set(after.sheets) - set(before.sheets)
    if new_sheets:
        console.print(f"[dim]new sheets in the output (exempt): {sorted(new_sheets)}[/dim]")
    # The writer appends rows to the two changelog sheets; cells that did not exist
    # in the input are expected there. Changes to cells that DID exist are not.
    logs = {cfg.workbook.changelog_sheet, cfg.workbook.changelog_detail_sheet}
    appended = {key for key in after.values if key[0] in logs and key not in before.values}
    if appended:
        console.print(f"[dim]{len(appended)} appended changelog cells (exempt)[/dim]")
    allowed = set(allowed) | appended

    problems = compare(
        before,
        after,
        allowed_value_changes=allowed,
        allowed_new_sheets=new_sheets,
        allowed_autofilter_changes={cfg.workbook.changelog_detail_sheet},
    )
    console.print(format_report(problems))
    return 1 if problems else 0


def write_duplicates_report(cfg, view, run_id: str) -> tuple[Path, int]:
    """Emit `data/out/duplicates_<run_id>.md`. Detection only -- never a merge."""
    pairs = find_duplicates(view, cfg)
    directory = cfg.artifacts_directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"duplicates_{run_id}.md"
    path.write_text(render_duplicates_report(pairs, view, cfg), encoding="utf-8")
    return path, len(pairs)


def announce(cfg, paths: dict[str, Path], workbook: Path | None = None) -> None:
    """Print where everything landed, split by destination.

    The two locations are kept visibly apart on purpose: the Drive folder receives
    the workbook and nothing else, and every process artifact stays local.
    """
    console.print()
    console.print("[bold]Google Drive[/bold] - the workbook only")
    console.print(f"  [dim]{cfg.output_directory}[/dim]")
    if workbook is not None:
        console.print(f"  [green]workbook[/green]    {workbook.name}")
    else:
        console.print("  [dim](nothing written - dry run)[/dim]")

    console.print()
    console.print("[bold]Local artifacts[/bold] - reports, CSVs, decision table")
    console.print(f"  [dim]{cfg.artifacts_directory}[/dim]")
    for label in ("report", "json", "changes", "review", "duplicates", "decisions"):
        if label in paths:
            console.print(f"  {label:11s} {paths[label].name}")


def cmd_duplicates(args: argparse.Namespace) -> int:
    """Report duplicate PARTNERS rows. Reads the workbook; changes nothing."""
    cfg = config_mod.load(args.config)
    view = load_workbook_view(
        cfg, args.workbook, reset_state=args.reset_state, command="efe duplicates"
    )
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    path, count = write_duplicates_report(cfg, view, run_id)
    console.print(
        f"[green]{count}[/green] duplicate pair(s) found across {len(view.rows)} rows. "
        "Nothing was changed."
    )
    console.print(f"  [dim]{path.parent}[/dim]")
    console.print(f"  duplicates  {path.name}")
    return 0


def _apply_target_overrides(cfg, args) -> None:
    """CLI targeting beats config for one run. 'all' clears a filter."""
    if getattr(args, "categories", None) is not None:
        cfg.selection.categories = (
            []
            if args.categories.strip().lower() == "all"
            else [f"{c.strip().rstrip('.')}." for c in args.categories.split(",") if c.strip()]
        )
    if getattr(args, "resorts", None) is not None:
        cfg.selection.resorts = (
            []
            if args.resorts.strip().lower() == "all"
            else [r.strip() for r in args.resorts.split(",") if r.strip()]
        )


def ledger_for(cfg, round_id: str, run_id: str, *, dry_run: bool) -> RunLedger:
    """The resume ledger for this run. A dry run keeps its own, so it never marks
    rows as done for the real run that follows it."""
    return RunLedger(cfg.state_directory, f"{round_id}-dryrun" if dry_run else round_id, run_id)


def report_resumed(selected: int, processed: int, ledger: RunLedger, round_id: str) -> int:
    """Say out loud how many rows the resume ledger skipped; return that count."""
    resumed = max(0, selected - processed)
    if resumed:
        console.print(
            f"  [yellow]{resumed} of {selected} rows skipped: already completed in the "
            f"resume ledger for round {round_id} ({ledger.progress_path.name}). "
            "Pass --fresh to redo them.[/yellow]"
        )
    return resumed


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    _apply_target_overrides(cfg, args)
    started_at = datetime.now()
    run_id = started_at.strftime("%Y%m%d-%H%M%S")
    round_id = args.round_id or cfg.selection.round_tag

    view = load_workbook_view(
        cfg,
        args.workbook,
        reset_state=args.reset_state,
        command="efe enrich",
        record_state=False,  # recorded below, once the run may proceed
    )
    # Fail on a version conflict before any fetching happens, not after an hour.
    destination = None if args.dry_run else next_version_path(cfg, view.version)
    record_input_state(cfg, view, "efe enrich")

    candidates, skipped = select_candidates(view, cfg)
    selected_total = len(candidates)

    bounds = parse_rows(args.rows)
    if bounds:
        low, high = bounds
        candidates = [c for c in candidates if low <= c.row <= high]
    if args.limit is not None:
        candidates = candidates[: args.limit]

    console.print(
        f"[bold]{run_id}[/bold]  round={round_id}  {'DRY RUN' if args.dry_run else 'WRITE'}"
    )
    console.print(
        f"  input: {view.path.name} (v{view.version:02d}, {view.data_rows} data rows)"
        + (f"  ->  output: {destination.name}" if destination else "")
    )
    targets = []
    if cfg.selection.categories:
        targets.append(f"categories={','.join(cfg.selection.categories)}")
    if cfg.selection.resorts:
        targets.append(f"resorts={len(cfg.selection.resorts)} objetivo")
    console.print(
        f"  {selected_total} rows selected of {len(view.rows)}; "
        f"processing {len(candidates)} this run"
        + (f"  [dim]({'; '.join(targets)})[/dim]" if targets else "")
    )
    console.print(
        f"  politeness: 1 request / {cfg.fetch.per_domain_delay_seconds}s per domain, "
        f"{cfg.fetch.global_concurrency} concurrent, "
        f"max {cfg.fetch.max_pages_per_entity} pages per entity\n"
    )

    ledger = ledger_for(cfg, round_id, run_id, dry_run=args.dry_run)
    if args.fresh:
        ledger.reset()

    def on_progress(done: int, total: int, candidate, chosen, held) -> None:
        console.print(
            f"  [{done:>3}/{total}] row {candidate.row:>3} {candidate.name[:44]:44s} "
            f"[green]+{len(chosen)}[/green] [yellow]~{len(held)}[/yellow]"
        )

    outcome = asyncio.run(
        run_enrichment(
            cfg,
            view,
            candidates,
            round_id=round_id,
            run_id=run_id,
            use_cache=not args.no_cache,
            ledger=ledger,
            on_progress=on_progress,
        )
    )

    console.print()
    resumed = report_resumed(len(candidates), len(outcome.results), ledger, round_id)
    if args.dry_run:
        print_dry_run(console, cfg, outcome)

    summary = build_summary(
        cfg,
        view,
        outcome,
        run_id=run_id,
        round_id=round_id,
        started_at=started_at,
        dry_run=args.dry_run,
        selected=selected_total,
        skipped=skipped,
    )

    duplicates_path, duplicate_count = write_duplicates_report(cfg, view, run_id)

    if args.dry_run:
        stem = f"{today_iso()}_DRYRUN_{run_id}"
        written = write_outputs(cfg.artifacts_directory, stem, summary, outcome)
        written["duplicates"] = duplicates_path
        print_summary(console, summary)
        console.print("\n[yellow]Dry run: no workbook was written.[/yellow]")
        if duplicate_count:
            console.print(
                f"[yellow]{duplicate_count} duplicate row pair(s) found[/yellow] "
                "- reported, never merged."
            )
        announce(cfg, written)
        return 0

    if not outcome.changes:
        print_summary(console, summary)
        if resumed and not outcome.results:
            console.print(
                "\n[yellow]Nothing was processed, so no workbook was emitted: every "
                "selected row is marked complete in the resume ledger. Pass --fresh "
                "to redo them.[/yellow]"
            )
        else:
            console.print(
                "\n[yellow]Nothing met the write threshold, so no workbook was emitted.[/yellow]"
            )
        stem = f"{today_iso()}_NOWRITE_{run_id}"
        written = write_outputs(cfg.artifacts_directory, stem, summary, outcome)
        written["duplicates"] = duplicates_path
        announce(cfg, written)
        return 0

    written_path = write_enriched(
        cfg,
        view,
        outcome.changes,
        run_id=run_id,
        held_back=outcome.held,
        output_path=destination,
    )
    summary.workbook_out = str(written_path)
    # The emitted file is what the resolver reads next; record it as the baseline.
    save_state(
        state_path(cfg.state_directory),
        WorkbookState.now(
            file=written_path.name,
            version=view.version + 1,
            data_rows=view.data_rows,
            header=view.header,
            command="efe enrich (output)",
            file_sha256=file_fingerprint(written_path),
        ),
    )

    # Artifacts are named after the workbook version they describe, but stay local.
    outputs = write_outputs(cfg.artifacts_directory, written_path.stem, summary, outcome)
    outputs["duplicates"] = duplicates_path
    print_summary(console, summary)
    if duplicate_count:
        console.print(
            f"\n[yellow]{duplicate_count} duplicate row pair(s) found[/yellow] "
            "- reported, never merged."
        )
    announce(cfg, outputs, workbook=written_path)
    console.print(f"\n[dim]input untouched: {view.path}[/dim]")
    return 0


def _promotion_report(
    cfg, run_id: str, source: Path, view, plan, *, status: str, prefix: str
) -> Path:
    """`<prefix>promotion_<run>.md`: the status first, then what was (or would be) done."""
    directory = cfg.artifacts_directory
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{prefix}promotion_{run_id}.md"
    heading = "Appended" if status.startswith("WRITTEN") else "Planned"
    lines = [
        f"# Promotion {run_id}",
        "",
        f"**{status}**",
        "",
        f"Source: `{source}`",
        f"Input: `{view.path.name}` (v{view.version:02d}, {view.data_rows} data rows)",
        "",
        f"## {heading} ({len(plan.accepted)})",
        "",
        "| Row | ID | Entity | Website |",
        "|---|---|---|---|",
    ]
    for row_number, values in plan.accepted:
        lines.append(
            f"| {row_number} | {values.get('ID', '')} | {values.get('Entity_Name', '')} | "
            f"{values.get('Website_URL', '')} |"
        )
    lines += [
        "",
        f"## Left out ({len(plan.rejected)})",
        "",
        "| ID | Entity | Why |",
        "|---|---|---|",
    ]
    for entity_id, name, reason in plan.rejected:
        lines.append(f"| {entity_id} | {name} | {reason} |")
    if plan.notices:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in plan.notices]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def cmd_promote(args: argparse.Namespace) -> int:
    """Append candidate rows (discovery output) as a new workbook version."""
    cfg = config_mod.load(args.config)
    started_at = datetime.now()
    run_id = started_at.strftime("%Y%m%d-%H%M%S")
    source = Path(args.candidates)
    if not source.is_file():
        raise SystemExit(f"candidates file not found: {source}")

    view = load_workbook_view(
        cfg,
        args.workbook,
        reset_state=args.reset_state,
        command="efe promote",
        record_state=False,
    )
    destination = None if args.dry_run else next_version_path(cfg, view.version)
    record_input_state(cfg, view, "efe promote")

    rows = read_candidates(source, cfg)
    plan = plan_promotion(view, cfg, rows, source, inspect_ranges(view.path, cfg))
    console.print(f"[bold]{run_id}[/bold]  {'DRY RUN' if args.dry_run else 'WRITE'}")
    console.print(
        f"  input: {view.path.name} (v{view.version:02d}, {view.data_rows} data rows)"
        + (f"  ->  output: {destination.name}" if destination else "")
    )
    console.print(f"  {len(rows)} candidates in {source.name}")
    console.print("  " + plan.describe().replace("\n", "\n  "))

    if not plan.accepted:
        console.print(
            "\n[yellow]Nothing to promote: every candidate is already in the sheet or "
            "repeated. No workbook was written.[/yellow]"
        )
        report = _promotion_report(
            cfg,
            run_id,
            source,
            view,
            plan,
            status="NOTHING TO PROMOTE - nothing written",
            prefix="NOWRITE_",
        )
        announce(cfg, {"report": report})
        return 0

    if args.dry_run:
        report = _promotion_report(
            cfg, run_id, source, view, plan, status="DRY RUN - nothing written", prefix="DRYRUN_"
        )
        console.print("\n[yellow]Dry run: no workbook was written.[/yellow]")
        announce(cfg, {"report": report})
        return 0

    try:
        written_path = write_promoted(cfg, view, plan, run_id=run_id, output_path=destination)
    except WorkbookGuardError as exc:
        _promotion_report(
            cfg,
            run_id,
            source,
            view,
            plan,
            status=f"REFUSED - {str(exc).splitlines()[0]}",
            prefix="REFUSED_",
        )
        raise
    save_state(
        state_path(cfg.state_directory),
        WorkbookState.now(
            file=written_path.name,
            version=view.version + 1,
            data_rows=view.data_rows + len(plan.accepted),
            header=view.header,
            command="efe promote (output)",
            file_sha256=file_fingerprint(written_path),
        ),
    )
    report = _promotion_report(
        cfg, run_id, source, view, plan, status=f"WRITTEN: {written_path.name}", prefix=""
    )
    console.print(
        f"\n[green]{len(plan.accepted)} rows appended[/green] at worksheet rows "
        f"{plan.first_row}..{plan.last_row}; {len(plan.rejected)} left out."
    )
    announce(cfg, {"report": report}, workbook=written_path)
    console.print(f"\n[dim]input untouched: {view.path}[/dim]")
    return 0


def _fixup_report(
    cfg, run_id: str, view, plan, handoff: list[str], *, status: str, prefix: str
) -> Path:
    """`<prefix>fixup_<run>.md`: status first, then what changed and what did not."""
    directory = cfg.artifacts_directory
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{prefix}fixup_{run_id}.md"
    heading = "Applied" if status.startswith("WRITTEN") else "Planned"
    lines = [
        f"# Fixup {run_id}",
        "",
        f"**{status}**",
        "",
        f"Plan: `{plan.source}`",
        f"Input: `{view.path.name}` (v{view.version:02d}, {view.data_rows} data rows)",
        "",
        f"## {heading}: renumbered IDs ({len(plan.renumbers)})",
        "",
        "| Row | Entity | Old | New | Why |",
        "|---|---|---|---|---|",
    ]
    for record in plan.renumbers:
        lines.append(
            f"| {record.row} | {record.guard_value} | {record.old} | {record.new} | {record.why} |"
        )
    lines += [
        "",
        f"## {heading}: cell corrections ({len(plan.cells)})",
        "",
        "| Row | Column | Old | New | Why |",
        "|---|---|---|---|---|",
    ]
    for record in plan.cells:
        lines.append(
            f"| {record.row} | {record.column} | `{record.old}` | `{record.new}` | {record.why} |"
        )
    lines += ["", f"## Listed, not automated ({len(plan.review)})", ""]
    lines += [f"- {item}" for item in plan.review] or ["- (none)"]
    lines += ["", f"## Not fixed by this tool ({len(handoff)})", ""]
    lines += [f"{i}. {line}" for i, line in enumerate(handoff, start=1)] or ["- (nothing drifted)"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def cmd_fixup(args: argparse.Namespace) -> int:
    """Apply a reviewed plan of corrections as a new workbook version."""
    cfg = config_mod.load(args.config)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.plan is None and not args.propose:
        raise SystemExit("efe fixup needs a plan CSV, or --propose to draft one")
    if args.plan is not None and not args.plan.is_file():
        raise SystemExit(f"plan file not found: {args.plan}")

    view = load_workbook_view(
        cfg, args.workbook, reset_state=args.reset_state, command="efe fixup", record_state=False
    )
    ranges = inspect_sheet_ranges(view.path, cfg)
    handoff = render_sheets_handoff(ranges, cfg)

    reserved: set[str] = set()
    for source in args.reserve or []:
        if not source.is_file():
            raise SystemExit(f"--reserve file not found: {source}")
        reserved |= {row["ID"] for row in read_candidates(source, cfg) if row.get("ID")}
    if reserved:
        console.print(f"[dim]{len(reserved)} ID(s) held back by --reserve[/dim]")

    if args.propose:
        plan = propose_fixups(view, cfg, reserved_ids=reserved)
        path = write_fixup_plan(cfg.artifacts_directory / f"fixup_plan_{run_id}.csv", plan)
        plan.source = path
        console.print(f"[bold]{run_id}[/bold]  PROPOSE")
        console.print(
            f"  input: {view.path.name} (v{view.version:02d}, {view.data_rows} data rows)"
        )
        console.print("  " + plan.describe().replace("\n", "\n  "))
        report = _fixup_report(
            cfg,
            run_id,
            view,
            plan,
            handoff,
            status="PROPOSAL - nothing written",
            prefix="PROPOSED_",
        )
        _print_handoff(handoff)
        console.print(f"\n[yellow]Review the plan, then apply it:[/yellow] efe fixup {path}")
        announce(cfg, {"report": report, "changes": path})
        return 0

    plan = read_fixup_plan(args.plan, cfg)
    if args.max_cells is not None:
        import efe.workbook.fixup as fixup_mod

        fixup_mod.MAX_CELLS = args.max_cells
    assert_fixups_legal(plan, cfg, view)
    assert_ids_unique_after(
        plan, cfg, view, reserved_ids=reserved, allow_backfill=args.allow_backfill
    )

    destination = None if args.dry_run else next_version_path(cfg, view.version)
    record_input_state(cfg, view, "efe fixup")
    console.print(f"[bold]{run_id}[/bold]  {'DRY RUN' if args.dry_run else 'WRITE'}")
    console.print(
        f"  input: {view.path.name} (v{view.version:02d}, {view.data_rows} data rows)"
        + (f"  ->  output: {destination.name}" if destination else "")
    )
    console.print(f"  plan:  {plan.source.name}  ({len(plan.records)} records)")
    console.print("  " + plan.describe().replace("\n", "\n  "))
    _print_handoff(handoff)

    if args.dry_run:
        report = _fixup_report(
            cfg, run_id, view, plan, handoff, status="DRY RUN - nothing written", prefix="DRYRUN_"
        )
        console.print("\n[yellow]Dry run: no workbook was written.[/yellow]")
        announce(cfg, {"report": report})
        return 0

    try:
        written_path = write_fixups(
            cfg,
            view,
            plan,
            run_id=run_id,
            output_path=destination,
            skip_stale=args.skip_stale,
            handoff=handoff,
        )
    except WorkbookGuardError as exc:
        _fixup_report(
            cfg,
            run_id,
            view,
            plan,
            handoff,
            status=f"REFUSED - {str(exc).splitlines()[0]}",
            prefix="REFUSED_",
        )
        raise
    save_state(
        state_path(cfg.state_directory),
        WorkbookState.now(
            file=written_path.name,
            version=view.version + 1,
            data_rows=view.data_rows,
            header=view.header,
            command="efe fixup (output)",
            file_sha256=file_fingerprint(written_path),
        ),
    )
    report = _fixup_report(
        cfg, run_id, view, plan, handoff, status=f"WRITTEN: {written_path.name}", prefix=""
    )
    console.print(
        f"\n[green]{len(plan.renumbers)} ID(s) renumbered, {len(plan.cells)} cell(s) "
        f"normalised[/green]; {len(plan.review)} left for you."
    )
    announce(cfg, {"report": report}, workbook=written_path)
    console.print(f"\n[dim]input untouched: {view.path}[/dim]")
    return 0


def _print_handoff(handoff: list[str]) -> None:
    if not handoff:
        return
    console.print("\n[bold]NOT fixed by this tool[/bold] - do these in Google Sheets:")
    for index, line in enumerate(handoff, start=1):
        console.print(f"  {index}. {line}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    handlers = {
        "enrich": cmd_enrich,
        "check": cmd_check,
        "verify": cmd_verify,
        "duplicates": cmd_duplicates,
        "promote": cmd_promote,
        "fixup": cmd_fixup,
    }
    try:
        return handlers[args.command](args)
    except (DriveSyncError, WorkbookLockedError) as exc:
        # Environment problems. Stop and say what to do; never retry in a loop.
        console.print(f"\n[bold red]STOPPED[/bold red]\n{exc}")
        return 2
    except SchemaMismatchError as exc:
        console.print(f"\n[bold red]SCHEMA MISMATCH[/bold red]\n{exc}")
        return 3
    except VerificationError as exc:
        console.print(f"\n[bold red]VERIFICATION FAILED[/bold red]\n{exc}")
        return 4
    except WorkbookGuardError as exc:
        console.print(f"\n[bold red]REFUSED[/bold red]\n{exc}")
        return 5
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted. Progress is saved; re-run to resume where it stopped.[/yellow]"
        )
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
