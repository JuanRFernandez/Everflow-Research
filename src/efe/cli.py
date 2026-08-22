"""Command line entry point.

`efe <verb>`. Phase 0 ships `enrich`, `check` and `verify`; the Phase-1+ verbs
(`export`, `import`, `round start`, `report`) slot in beside them without
restructuring anything.
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
from efe.workbook.reader import load_workbook_view, select_candidates
from efe.workbook.verify import compare, format_report, snapshot
from efe.workbook.writer import next_version_path, write_enriched

console = Console()


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

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
        "--dry-run", action="store_true",
        help="report what WOULD change; write no workbook",
    )
    enrich.add_argument("--limit", type=int, default=None, help="process at most N rows")
    enrich.add_argument(
        "--rows", default=None, metavar="A:B",
        help="restrict to worksheet rows A to B inclusive, e.g. 2:21",
    )
    enrich.add_argument("--workbook", type=Path, default=None, help="override workbook path")
    enrich.add_argument("--round", dest="round_id", default=None, help="round id, e.g. R2")
    enrich.add_argument("--no-cache", action="store_true", help="refetch, ignore the page cache")
    enrich.add_argument(
        "--fresh", action="store_true", help="ignore the resume ledger and start over"
    )

    check = subparsers.add_parser(
        "check", help="validate the workbook is readable and matches the schema"
    )
    check.add_argument("--workbook", type=Path, default=None)

    duplicates = subparsers.add_parser(
        "duplicates",
        help="report duplicate PARTNERS rows (accent-insensitive); changes nothing",
    )
    duplicates.add_argument("--workbook", type=Path, default=None)

    verify = subparsers.add_parser(
        "verify", help="prove an emitted workbook is faithful to its input"
    )
    verify.add_argument("output", type=Path, help="the emitted file to check")
    verify.add_argument("--against", type=Path, required=True, help="the input it came from")
    verify.add_argument(
        "--allow-partners-changes", action="store_true",
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
# Commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    view = load_workbook_view(cfg, args.workbook)
    candidates, skipped = select_candidates(view, cfg)

    console.print(f"[green]OK[/green]  {view.path}")
    console.print(f"     {len(view.rows)} data rows, {view.formula_count} formulas")
    console.print(f"     {len(candidates)} rows selected for enrichment, {len(skipped)} skipped")
    expected = cfg.workbook.expected_formula_count
    if view.formula_count != expected:
        console.print(
            f"[yellow]WARN[/yellow] formula count is {view.formula_count}, "
            f"config expects {expected}"
        )
    for reason in sorted(set(skipped.values())):
        count = sum(1 for r in skipped.values() if r == reason)
        console.print(f"     [dim]skip x{count}: {reason}[/dim]")
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

    problems = compare(
        before, after, allowed_value_changes=allowed, allowed_new_sheets=new_sheets
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
    view = load_workbook_view(cfg, args.workbook)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    path, count = write_duplicates_report(cfg, view, run_id)
    console.print(
        f"[green]{count}[/green] duplicate pair(s) found across {len(view.rows)} rows. "
        "Nothing was changed."
    )
    console.print(f"  [dim]{path.parent}[/dim]")
    console.print(f"  duplicates  {path.name}")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    started_at = datetime.now()
    run_id = started_at.strftime("%Y%m%d-%H%M%S")
    round_id = args.round_id or cfg.selection.round_tag

    view = load_workbook_view(cfg, args.workbook)
    candidates, skipped = select_candidates(view, cfg)
    selected_total = len(candidates)

    bounds = parse_rows(args.rows)
    if bounds:
        low, high = bounds
        candidates = [c for c in candidates if low <= c.row <= high]
    if args.limit is not None:
        candidates = candidates[: args.limit]

    console.print(
        f"[bold]{run_id}[/bold]  round={round_id}  "
        f"{'DRY RUN' if args.dry_run else 'WRITE'}"
    )
    console.print(
        f"  {selected_total} rows selected of {len(view.rows)}; "
        f"processing {len(candidates)} this run"
    )
    console.print(
        f"  politeness: 1 request / {cfg.fetch.per_domain_delay_seconds}s per domain, "
        f"{cfg.fetch.global_concurrency} concurrent, "
        f"max {cfg.fetch.max_pages_per_entity} pages per entity\n"
    )

    ledger = RunLedger(cfg.state_directory, round_id, run_id)
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
    if args.dry_run:
        print_dry_run(console, cfg, outcome)

    summary = build_summary(
        cfg, view, outcome,
        run_id=run_id, round_id=round_id, started_at=started_at,
        dry_run=args.dry_run, selected=selected_total, skipped=skipped,
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
        console.print(
            "\n[yellow]Nothing met the write threshold, so no workbook was emitted.[/yellow]"
        )
        stem = f"{today_iso()}_NOWRITE_{run_id}"
        written = write_outputs(cfg.artifacts_directory, stem, summary, outcome)
        written["duplicates"] = duplicates_path
        announce(cfg, written)
        return 0

    destination = next_version_path(cfg)
    written_path = write_enriched(
        cfg, view, outcome.changes,
        run_id=run_id, held_back=outcome.held, output_path=destination,
    )
    summary.workbook_out = str(written_path)

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
            "\n[yellow]Interrupted. Progress is saved; re-run to resume "
            "where it stopped.[/yellow]"
        )
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
