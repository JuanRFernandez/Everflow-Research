"""Guarded writing of the enriched workbook.

The input file is never modified. A copy is built in the repo's gitignored `data/`
directory, verified cell by cell against the input, and only then copied to the
Google Drive folder under a new version number. If verification fails, the candidate
is deleted and the run reports what broke.

Three rules are enforced here rather than trusted to callers:

1. Only columns listed as `writable_columns` (plus the three provenance columns) can
   ever be written. A CRM column or a formula column is a hard error.
2. A cell that already holds a real value is never overwritten.
3. A value with no source URL, no fetch timestamp and no matched text is refused.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import column_index_from_string

from efe.config import Config
from efe.models import CellChange, VerificationError, today_iso
from efe.workbook.reader import WorkbookView, guard_readable, guard_writable
from efe.workbook.verify import compare, format_report, snapshot
from efe.workbook.xmlutil import reinject_cached_values

CHANGELOG_DETAIL_HEADERS = [
    "Timestamp",
    "Run_ID",
    "Row",
    "Entity_ID",
    "Entity_Name",
    "Column",
    "Field",
    "Old_Value",
    "New_Value",
    "Confidence",
    "Data_Class",
    "Source_URL",
    "Fetched_At",
    "Extractor",
    "Note",
]


# ---------------------------------------------------------------------------
# Output naming
# ---------------------------------------------------------------------------

def next_version_path(cfg: Config, when: str | None = None) -> Path:
    """`YYYY-MM-DD_EFE_Alpine_Partner_Database_vNN.xlsx`, one above the highest vNN.

    The whole directory is scanned rather than the input filename alone, so a version
    emitted on an earlier date is still respected.
    """
    directory = cfg.output_directory
    stamp = when or today_iso()
    basename = cfg.output_basename

    highest = 0
    if directory.is_dir():
        for entry in directory.glob(f"*{basename}_v*.xlsx"):
            tail = entry.stem.rsplit("_v", 1)[-1]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return directory / f"{stamp}_{basename}_v{highest + 1:02d}.xlsx"


# ---------------------------------------------------------------------------
# Guards on the change set
# ---------------------------------------------------------------------------

def assert_changes_legal(changes: list[CellChange], cfg: Config, view: WorkbookView) -> None:
    """Refuse a change set that would break any of the three standing rules."""
    spec = cfg.workbook
    writable = set(spec.writable_columns) | set(spec.provenance_columns)
    forbidden = set(spec.crm_columns) | set(spec.formula_columns)
    by_row = {pr.row: pr for pr in view.rows}
    problems: list[str] = []

    seen: set[tuple[int, str]] = set()
    for change in changes:
        target = (change.row, change.column)
        if change.column in forbidden:
            problems.append(
                f"{change.column}{change.row} is a human-owned CRM or formula column "
                f"({change.field}) - refusing to write"
            )
            continue
        if change.column not in writable:
            problems.append(
                f"{change.column}{change.row} is not in writable_columns ({change.field})"
            )
            continue
        if target in seen:
            problems.append(f"{change.column}{change.row} written twice in one run")
            continue
        seen.add(target)

        row = by_row.get(change.row)
        if row is None:
            problems.append(f"row {change.row} is not a PARTNERS data row")
            continue
        current = row.get(change.column)
        if change.column in spec.writable_columns and not spec.is_empty(current):
            problems.append(
                f"{change.column}{change.row} already holds {current!r} "
                "- refusing to overwrite a non-TBD value"
            )
        if not change.source_url or not change.source_url.startswith(("http://", "https://")):
            problems.append(
                f"{change.column}{change.row} has no usable source URL "
                f"({change.source_url!r}) - refusing to write a value without provenance"
            )

    if problems:
        joined = "\n  - ".join(problems)
        raise VerificationError(
            "The change set is illegal and was NOT written:\n  - " + joined
        )


def assert_no_precedents_touched(changes: list[CellChange], cfg: Config) -> None:
    """Confirm no written cell feeds any formula in the workbook.

    This is the premise that makes cached-value reinjection sound. In this workbook
    the only cross-sheet dependency is DASHBOARD -> PARTNERS!{C,G,Y,Z,AI}, and
    PARTNERS!AC depends on AA and AB. If a future config makes one of those columns
    writable, reinjection would preserve a stale result, so the run stops instead.
    """
    spec = cfg.workbook
    precedents = {
        spec.column_for("category"),        # C  -> DASHBOARD COUNTIF
        spec.column_for("country"),         # G  -> DASHBOARD COUNTIF
        spec.column_for("priority_score"),  # Y  -> DASHBOARD COUNTIFS
        spec.column_for("contacted"),       # Z  -> DASHBOARD COUNTIFS
        spec.column_for("status"),          # AI -> DASHBOARD COUNTIF
        "AA",                               # -> PARTNERS!AC
        "AB",                               # -> PARTNERS!AC
    }
    offending = sorted({c.column for c in changes} & precedents)
    if offending:
        raise VerificationError(
            f"Columns {offending} feed live formulas. Writing them would invalidate the "
            "cached results this writer preserves. Nothing has been written."
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def build_provenance_changes(
    changes: list[CellChange], cfg: Config, view: WorkbookView, run_date: str
) -> list[CellChange]:
    """AK/AL/AM updates for rows that actually changed. Untouched rows stay untouched.

    `Date_Verified` is taken from the evidence, not from the clock: it is the date the
    page carrying the value was actually fetched. A long run that crosses midnight,
    or one replayed from cache days later, would otherwise stamp rows with a date on
    which nothing was verified. `run_date` is only the fallback.
    """
    spec = cfg.workbook
    ak = spec.column_for("source_url")
    al = spec.column_for("date_verified")
    am = spec.column_for("round")
    by_row = {pr.row: pr for pr in view.rows}

    urls_by_row: dict[int, list[str]] = defaultdict(list)
    meta_by_row: dict[int, CellChange] = {}
    verified_on: dict[int, str] = {}
    for change in changes:
        if change.source_url not in urls_by_row[change.row]:
            urls_by_row[change.row].append(change.source_url)
        meta_by_row.setdefault(change.row, change)
        fetched = change.fetched_at.date().isoformat()
        # The most recent page that contributed a value to this row.
        if fetched > verified_on.get(change.row, ""):
            verified_on[change.row] = fetched

    out: list[CellChange] = []
    for row_number, urls in sorted(urls_by_row.items()):
        row = by_row[row_number]
        sample = meta_by_row[row_number]

        existing = row.get(ak)
        joined = "; ".join(urls)
        new_source = joined if spec.is_empty(existing) else f"{existing}; {joined}"

        for column, old, new, field in (
            (ak, existing, new_source, "Source_URL"),
            (al, row.get(al), verified_on.get(row_number, run_date), "Date_Verified"),
            (am, row.get(am), cfg.selection.round_tag, "Round"),
        ):
            if str(old) == str(new):
                continue
            out.append(
                CellChange(
                    row=row_number,
                    column=column,
                    field=field,
                    entity_id=sample.entity_id,
                    entity_name=sample.entity_name,
                    old_value=str(old),
                    new_value=str(new),
                    confidence=sample.confidence,
                    data_class=sample.data_class,
                    source_url=sample.source_url,
                    fetched_at=sample.fetched_at,
                    extractor="provenance",
                    note="provenance updated because this row gained at least one value",
                )
            )
    return out


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------

def _append_changelog(ws, version_label: str, run_date: str, message: str) -> list[str]:
    """Append one version-history row; return the coordinates written."""
    row = ws.max_row + 1
    written = []
    for offset, value in enumerate((version_label, run_date, message, "efe enrich (Phase 0)")):
        cell = ws.cell(row, 1 + offset)
        cell.value = value
        cell.alignment = Alignment(vertical="top", wrap_text=offset == 2)
        written.append(cell.coordinate)
    return written


def _write_changelog_detail(wb, sheet_name: str, records: list[CellChange],
                            run_id: str) -> list[str]:
    """Create the audit sheet: one row per cell change and per held-back candidate."""
    ws = wb.create_sheet(sheet_name)
    written: list[str] = []

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    for index, title in enumerate(CHANGELOG_DETAIL_HEADERS, start=1):
        cell = ws.cell(1, index)
        cell.value = title
        cell.font = header_font
        cell.fill = header_fill
        written.append(cell.coordinate)

    for offset, change in enumerate(records, start=2):
        values = [
            datetime.now().isoformat(timespec="seconds"),
            run_id,
            change.row,
            change.entity_id,
            change.entity_name,
            change.column,
            change.field,
            change.old_value,
            change.new_value,
            change.confidence.value,
            change.data_class.value,
            change.source_url,
            change.fetched_at.isoformat(timespec="seconds"),
            change.extractor,
            change.note,
        ]
        for index, value in enumerate(values, start=1):
            cell = ws.cell(offset, index)
            cell.value = value
            written.append(cell.coordinate)

    widths = {"A": 20, "B": 14, "C": 6, "D": 11, "E": 34, "F": 7, "G": 22,
              "H": 16, "I": 34, "J": 11, "K": 15, "L": 46, "M": 20, "N": 20, "O": 52}
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:O{max(1, len(records) + 1)}"
    return written


def write_enriched(
    cfg: Config,
    view: WorkbookView,
    changes: list[CellChange],
    *,
    run_id: str,
    held_back: list[CellChange] | None = None,
    changelog_message: str = "",
    output_path: Path | None = None,
    workdir: Path | None = None,
) -> Path:
    """Emit a verified, enriched copy of the workbook. Returns the written path.

    Raises:
        VerificationError: the candidate failed the fidelity gate and was discarded.
        WorkbookLockedError / DriveSyncError: the environment is not safe to write.
    """
    source = view.path
    guard_readable(source, cfg.workbook.min_plausible_bytes)

    destination = output_path or next_version_path(cfg)
    guard_writable(destination)

    run_date = today_iso()
    assert_changes_legal(changes, cfg, view)
    assert_no_precedents_touched(changes, cfg)

    provenance = build_provenance_changes(changes, cfg, view, run_date)
    assert_changes_legal(changes + provenance, cfg, view)

    staging_dir = workdir or (cfg.state_directory / "staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    candidate = staging_dir / destination.name

    if candidate.exists():
        candidate.unlink()
    shutil.copy2(source, candidate)

    allowed: set[tuple[str, str]] = set()
    sheet_name = cfg.workbook.sheet
    version_label = destination.stem.rsplit("_", 1)[-1]

    try:
        wb = load_workbook(candidate, data_only=False)
        try:
            ws = wb[sheet_name]
            for change in changes + provenance:
                cell = ws.cell(change.row, column_index_from_string(change.column))
                cell.value = change.new_value
                allowed.add((sheet_name, cell.coordinate))

            filled = len(changes)
            rows_touched = len({c.row for c in changes})
            message = changelog_message or (
                f"Contact enrichment (efe enrich, run {run_id}). "
                f"{filled} cells filled across {rows_touched} rows from published "
                f"company pages; every value carries a source URL and fetch date in "
                f"{cfg.workbook.changelog_detail_sheet}. "
                f"{len(held_back or [])} candidates held for review, not written. "
                "No CRM column, pre-existing value or formula was modified."
            )
            for coordinate in _append_changelog(
                wb[cfg.workbook.changelog_sheet], version_label, run_date, message
            ):
                allowed.add((cfg.workbook.changelog_sheet, coordinate))

            detail_records = sorted(
                (changes + provenance + list(held_back or [])),
                key=lambda c: (c.row, c.column, c.field),
            )
            for coordinate in _write_changelog_detail(
                wb, cfg.workbook.changelog_detail_sheet, detail_records, run_id
            ):
                allowed.add((cfg.workbook.changelog_detail_sheet, coordinate))

            wb.save(candidate)
        finally:
            wb.close()

        repaired = reinject_cached_values(candidate, source)

        problems = compare(
            snapshot(source),
            snapshot(candidate),
            allowed_value_changes=allowed,
            allowed_new_sheets={cfg.workbook.changelog_detail_sheet},
            require_cached_values=True,
        )
        if problems:
            raise VerificationError(
                format_report(problems)
                + f"\n\nThe candidate output was DELETED. {source.name} is untouched."
            )
        if repaired == 0 and view.formula_count:
            raise VerificationError(
                "No cached formula results were reinjected although the workbook has "
                f"{view.formula_count} formulas. The candidate output was DELETED."
            )

        guard_writable(destination)
        shutil.copy2(candidate, destination)
        return destination
    finally:
        if candidate.exists():
            candidate.unlink()
