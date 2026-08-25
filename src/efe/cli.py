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
from efe.workbook.reader import (
    WorkbookView,
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
    verify.add_argument(
        "--allow-partners-changes",
        action="store_true",
        help="tolerate value changes on PARTNERS (they are expected after an enrich)",
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
        console.print(
            f"  formula   [green]OK[/green]  {name} ({spec.letter_of(name)}) holds a "
            f"formula on all {view.data_rows} data rows"
        )

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
        else:
            console.print(
                f"  status    [green]OK[/green]  v{view.version:02d} with {view.data_rows} "
                "rows is not behind the baseline"
            )
    console.print(f"  recorded  [dim]{state_path(cfg.state_directory)}[/dim]")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    # The report below prints the resolution in full; keep the log line out of it.
    logging.getLogger("efe.workbook.resolve").setLevel(logging.WARNING)
    logging.getLogger("efe.workbook.reader").setLevel(logging.WARNING)
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
