"""Promotion: candidate rows become PARTNERS rows.

Discovery emits candidates as a CSV shaped like PARTNERS (`scripts/discover_hotels.py`).
Promotion appends the ones that are not already in the sheet -- by ID, by domain, by
folded name -- as new data rows after the last real row, gives each the live
`Next_Follow_Up` formula the sheet expects on every data row, records the event in
CHANGELOG / CHANGELOG_DETAIL, and emits the next version through the same fidelity
gate as enrichment. No existing cell is ever touched: every write lands on a row
number above the last data row of the input.
"""

from __future__ import annotations

import csv
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string

from efe.config import Config
from efe.dedupe import fold
from efe.models import CellChange, Confidence, DataClass, VerificationError, today_iso
from efe.workbook.reader import (
    WorkbookView,
    domain_of,
    file_fingerprint,
    guard_readable,
    guard_writable,
)
from efe.workbook.resolve import parse_version
from efe.workbook.verify import compare, format_report, snapshot
from efe.workbook.writer import (
    _append_changelog,
    _write_changelog_detail,
    assert_version_free,
    next_version_path,
)
from efe.workbook.xmlutil import read_cached_values, reinject_cached_values

log = logging.getLogger(__name__)


@dataclass
class PromotionPlan:
    """What would be appended, what would not, and why."""

    source: Path
    first_row: int
    #: (worksheet row, header name -> value) for every accepted candidate, in order.
    accepted: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    #: (ID, Entity_Name, reason) for every candidate left out.
    rejected: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [row.get("ID", "") for _, row in self.accepted]

    def describe(self) -> str:
        lines = [
            f"promotion from {self.source.name}: {len(self.accepted)} row(s) to append "
            f"at worksheet rows {self.first_row}..{self.first_row + len(self.accepted) - 1}, "
            f"{len(self.rejected)} left out"
        ]
        for entity_id, name, reason in self.rejected:
            lines.append(f"  skipped : {entity_id} {name} -- {reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading and planning
# ---------------------------------------------------------------------------


def read_candidates(path: Path, cfg: Config) -> list[dict[str, str]]:
    """Rows of a PARTNERS-shaped CSV, keyed by header name.

    Every CSV column must be a PARTNERS column; PARTNERS columns the CSV lacks (a
    column added to the sheet after the CSV was made) are filled blank.
    """
    spec = cfg.workbook
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    unknown = [c for c in columns if c not in spec.header]
    if unknown:
        raise VerificationError(
            f"{path.name} has columns that are not PARTNERS columns: {unknown}. "
            "Nothing has been written."
        )
    for required in ("ID", "Entity_Name"):
        if required not in columns:
            raise VerificationError(
                f"{path.name} lacks the {required!r} column. Nothing has been written."
            )
    return [{name: (row.get(name) or "").strip() for name in spec.header} for row in rows]


def plan_promotion(
    view: WorkbookView, cfg: Config, rows: list[dict[str, str]], source: Path
) -> PromotionPlan:
    """Decide which candidates may become rows. Detection only: nothing is merged."""
    spec = cfg.workbook
    id_col = spec.column_for("id")
    name_col = spec.column_for("entity_name")
    web_col = spec.column_for("website_url")
    existing_ids = {pr.get(id_col) for pr in view.rows}
    existing_domains: dict[str, int] = {}
    existing_names: dict[str, int] = {}
    for pr in view.rows:
        d = domain_of(pr.get(web_col))
        if d and d not in existing_domains:
            existing_domains[d] = pr.row
        n = fold(pr.get(name_col))
        if n and n not in existing_names:
            existing_names[n] = pr.row

    last_row = view.schema.last_row if view.schema else (view.rows[-1].row if view.rows else 1)
    plan = PromotionPlan(source=source, first_row=last_row + 1)
    seen_ids: set[str] = set()
    seen_domains: set[str] = set()
    seen_names: set[str] = set()
    next_row = last_row + 1
    for row in rows:
        entity_id, name = row.get("ID", ""), row.get("Entity_Name", "")
        domain = domain_of(row.get("Website_URL", ""))
        folded = fold(name)
        reason = ""
        if not entity_id or not name:
            reason = "ID or Entity_Name missing"
        elif entity_id in existing_ids:
            reason = f"ID {entity_id} already in PARTNERS"
        elif entity_id in seen_ids:
            reason = f"ID {entity_id} repeated in the CSV"
        elif domain and domain in existing_domains:
            reason = f"domain {domain} already in PARTNERS (row {existing_domains[domain]})"
        elif folded in existing_names:
            reason = f"name already in PARTNERS (row {existing_names[folded]})"
        elif domain and domain in seen_domains:
            reason = f"domain {domain} repeated in the CSV"
        elif folded in seen_names:
            reason = "name repeated in the CSV"
        if reason:
            plan.rejected.append((entity_id, name, reason))
            continue
        seen_ids.add(entity_id)
        if domain:
            seen_domains.add(domain)
        seen_names.add(folded)
        plan.accepted.append((next_row, row))
        next_row += 1
    return plan


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _cell_value(text: str):
    """CSV text -> cell value: blanks stay empty, plain integers become numbers."""
    if text == "":
        return None
    if text.isdigit():
        return int(text)
    return text


def _formula_template(ws, spec, last_row: int) -> dict[str, tuple[str, str]]:
    """Formula column -> (origin coordinate, formula) taken from the nearest data row
    above `last_row` that holds one. Translated per new row by openpyxl."""
    out: dict[str, tuple[str, str]] = {}
    for name in spec.formula_columns:
        letter = spec.letter_of(name)
        col = column_index_from_string(letter)
        for r in range(last_row, spec.first_data_row - 1, -1):
            value = ws.cell(r, col).value
            if isinstance(value, str) and value.startswith("="):
                out[name] = (f"{letter}{r}", value)
                break
        else:
            raise VerificationError(
                f"no data row holds a formula in {name} ({letter}); cannot derive the "
                "formula for the new rows. Nothing has been written."
            )
    return out


def write_promoted(
    cfg: Config,
    view: WorkbookView,
    plan: PromotionPlan,
    *,
    run_id: str,
    output_path: Path | None = None,
    workdir: Path | None = None,
) -> Path:
    """Append the accepted rows and emit the next version. Returns the written path.

    Raises:
        VerificationError: the fidelity gate failed and the candidate was discarded.
        VersionConflictError: the next version already exists somewhere.
    """
    if not plan.accepted:
        raise VerificationError(
            "nothing to promote: every candidate was left out. Nothing has been written."
        )
    spec = cfg.workbook
    source = view.path
    guard_readable(source, spec.min_plausible_bytes)
    if view.fingerprint and file_fingerprint(source) != view.fingerprint:
        raise VerificationError(
            f"The input workbook changed since it was read:\n  {source}\n"
            "Re-run from the current file. Nothing has been written."
        )
    last_row = view.schema.last_row if view.schema else view.rows[-1].row
    if plan.first_row <= last_row:
        raise VerificationError(
            f"promotion would start at row {plan.first_row}, but data runs to row "
            f"{last_row}. Nothing has been written."
        )

    destination = output_path or next_version_path(cfg, view.version)
    target_version = parse_version(destination, cfg.output_basename) or view.version + 1
    assert_version_free(cfg, target_version)
    guard_writable(destination)

    staging_dir = workdir or (cfg.state_directory / "staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    candidate = staging_dir / f"{run_id}_{destination.name}"
    if candidate.exists():
        candidate.unlink()
    shutil.copy2(source, candidate)

    allowed: set[tuple[str, str]] = set()
    sheet_name = spec.sheet
    detail_sheet = spec.changelog_detail_sheet
    run_date = today_iso()
    version_label = destination.stem.rsplit("_", 1)[-1]
    detail_created = False
    now = datetime.now()

    try:
        wb = load_workbook(candidate, data_only=False)
        try:
            ws = wb[sheet_name]
            templates = _formula_template(ws, spec, last_row)
            audit: list[CellChange] = []
            for row_number, values in plan.accepted:
                for name in spec.header:
                    if name in templates:
                        continue
                    value = _cell_value(values.get(name, ""))
                    if value is None:
                        continue
                    if isinstance(value, str) and value.startswith("="):
                        raise VerificationError(
                            f"{values.get('ID')} {name}: value {value!r} would be stored as a "
                            "formula. Nothing has been written."
                        )
                    cell = ws.cell(row_number, column_index_from_string(spec.letter_of(name)))
                    cell.value = value
                    allowed.add((sheet_name, cell.coordinate))
                for name, (origin, formula) in templates.items():
                    letter = spec.letter_of(name)
                    cell = ws.cell(row_number, column_index_from_string(letter))
                    cell.value = Translator(formula, origin=origin).translate_formula(
                        f"{letter}{row_number}"
                    )
                    allowed.add((sheet_name, cell.coordinate))
                audit.append(
                    CellChange(
                        row=row_number,
                        column=spec.column_for("id"),
                        field="row",
                        entity_id=values.get("ID", ""),
                        entity_name=values.get("Entity_Name", ""),
                        old_value="",
                        new_value=f"row promoted from {plan.source.name}",
                        confidence=Confidence.HIGH,
                        data_class=DataClass.CORPORATE_ROLE,
                        source_url=values.get("Source_URL", ""),
                        fetched_at=now,
                        extractor="promote",
                        note=values.get("Strategic_Fit_Note", ""),
                    )
                )

            ids = plan.ids
            message = (
                f"Promotion (efe promote, run {run_id}): {len(plan.accepted)} candidate rows "
                f"appended from {plan.source.name} ({ids[0]}..{ids[-1]}) at rows "
                f"{plan.first_row}..{plan.first_row + len(plan.accepted) - 1}; "
                f"{len(plan.rejected)} left out as already present or repeated. "
                "Contacts stay TBD for the enricher; no existing cell was modified."
            )
            for coordinate in _append_changelog(
                wb[spec.changelog_sheet], version_label, run_date, message, author="efe promote"
            ):
                allowed.add((spec.changelog_sheet, coordinate))
            coordinates, detail_created = _write_changelog_detail(wb, detail_sheet, audit, run_id)
            for coordinate in coordinates:
                allowed.add((detail_sheet, coordinate))
            wb.save(candidate)
        finally:
            wb.close()

        repaired = reinject_cached_values(candidate, source)
        problems = compare(
            snapshot(source),
            snapshot(candidate),
            allowed_value_changes=allowed,
            allowed_new_sheets={detail_sheet} if detail_created else set(),
            allowed_autofilter_changes={detail_sheet},
            require_cached_values=True,
        )
        if problems:
            raise VerificationError(
                format_report(problems)
                + f"\n\nThe candidate output was DELETED. {source.name} is untouched."
            )
        source_cached = sum(1 for v in read_cached_values(source).values() if v is not None)
        if repaired == 0 and source_cached:
            raise VerificationError(
                "No cached formula results were reinjected although the input carries "
                f"{source_cached}. The candidate output was DELETED."
            )

        assert_version_free(cfg, target_version)
        guard_writable(destination)
        shutil.copy2(candidate, destination)
        return destination
    finally:
        if candidate.exists():
            candidate.unlink()
