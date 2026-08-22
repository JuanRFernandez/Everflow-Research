"""Guarded reading of the partner workbook.

Two environment failure modes get their own hard stop, because retrying either one
in a loop makes things worse rather than better:

1. Google Drive hands back a zero-byte file or a placeholder stub.
2. The workbook is open in Excel and therefore exclusively locked.

Both raise immediately with an instruction, never a retry.
"""

from __future__ import annotations

import logging
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.workbook.workbook import Workbook

from efe.config import Config
from efe.models import (
    Candidate,
    DriveSyncError,
    SchemaMismatchError,
    WorkbookLockedError,
)

log = logging.getLogger(__name__)

#: A workbook this small cannot be the real thing; Drive is still syncing.
MIN_PLAUSIBLE_BYTES = 50_000

#: Logical fields the enricher can fill, in workbook column order.
ENRICHABLE_FIELDS = (
    "general_email",
    "sales_b2b_email",
    "phone",
    "whatsapp",
    "contact_person_name",
    "contact_person_role",
    "linkedin_url",
    "instagram_handle",
    "commission_terms",
)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _lock_file_for(path: Path) -> Path:
    """Excel's owner file: `Book.xlsx` -> `~$Book.xlsx` in the same directory."""
    return path.with_name("~$" + path.name)


def guard_readable(path: Path, min_bytes: int = MIN_PLAUSIBLE_BYTES) -> None:
    """Stop the run if the workbook cannot be trusted to be the real file.

    Args:
        path: the workbook to check.
        min_bytes: size below which the file must be a Drive placeholder rather
            than the real thing. Comes from `workbook.min_plausible_bytes`.

    Raises:
        DriveSyncError: missing, empty, truncated, or not a valid xlsx container.
        WorkbookLockedError: Excel holds an exclusive lock on it.
    """
    if not path.exists():
        raise DriveSyncError(
            f"Workbook not found:\n  {path}\n"
            "If this path is on Google Drive, the file may not be synced to this "
            "machine yet. Open the Drive folder, wait for the sync to finish, then "
            "re-run. Nothing has been changed."
        )

    size = path.stat().st_size
    if size == 0:
        raise DriveSyncError(
            f"Workbook read back as ZERO BYTES:\n  {path}\n"
            "This is a Google Drive sync problem, not a data problem. Let Drive "
            "finish syncing (the file should be ~100 KB), then re-run. "
            "Nothing has been changed."
        )
    if size < min_bytes:
        raise DriveSyncError(
            f"Workbook is only {size:,} bytes:\n  {path}\n"
            f"That is below the {min_bytes:,}-byte floor for this file and "
            "looks like a Drive placeholder or a truncated download. Let Drive "
            "finish syncing, then re-run. Nothing has been changed."
        )

    lock = _lock_file_for(path)
    if lock.exists():
        raise WorkbookLockedError(
            f"The workbook is OPEN IN EXCEL:\n  {path}\n"
            f"Excel's lock file is present ({lock.name}). Close the workbook in "
            "Excel and re-run. Nothing has been changed."
        )

    try:
        with path.open("rb") as fh:
            fh.read(4)
    except PermissionError as exc:
        raise WorkbookLockedError(
            f"The workbook is LOCKED and cannot be read:\n  {path}\n"
            "It is almost certainly open in Excel. Close it and re-run. "
            "Nothing has been changed."
        ) from exc

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise DriveSyncError(
            f"Workbook is not a readable .xlsx container:\n  {path}\n"
            "Drive may have delivered a placeholder or a partial file. Let the sync "
            "finish, then re-run. Nothing has been changed."
        ) from exc

    if "xl/workbook.xml" not in names:
        raise DriveSyncError(
            f"Workbook container is missing xl/workbook.xml:\n  {path}\n"
            "The file is corrupt or still syncing. Nothing has been changed."
        )


def guard_writable(path: Path) -> None:
    """Stop before writing if the destination is locked or already taken."""
    if path.exists():
        raise WorkbookGuardOutputExists(
            f"Refusing to overwrite an existing file:\n  {path}\n"
            "Version numbers only ever go up. Nothing has been changed."
        )
    lock = _lock_file_for(path)
    if lock.exists():
        raise WorkbookLockedError(
            f"The intended output file is OPEN IN EXCEL:\n  {path}\n"
            "Close it and re-run. Nothing has been changed."
        )
    parent = path.parent
    if not parent.is_dir():
        raise DriveSyncError(
            f"Output directory does not exist:\n  {parent}\n"
            "If this is a Google Drive path, check the folder is synced offline."
        )


class WorkbookGuardOutputExists(RuntimeError):
    """The versioned output filename is already taken."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def last_data_row(ws, spec) -> int:
    """The last row that actually holds an entity.

    `ws.max_row` is the used range, not the data. Excel and Google Sheets both pad a
    saved sheet out to their default grid -- a round trip through Sheets left this
    workbook reporting 1000 rows with 746 of them empty. Trailing blanks are
    cosmetic; the data extent is what the schema check cares about.
    """
    id_col = column_index_from_string(spec.column_for("id"))
    name_col = column_index_from_string(spec.column_for("entity_name"))
    for row in range(ws.max_row, spec.first_data_row - 1, -1):
        if str(ws.cell(row, id_col).value or "").strip() or str(
            ws.cell(row, name_col).value or ""
        ).strip():
            return row
    return spec.header_row



def assert_schema(wb: Workbook, cfg: Config) -> None:
    """Confirm the workbook is the shape the config describes.

    Reading a workbook whose columns have moved and writing to hardcoded letters is
    the one mistake that silently destroys data. This makes it impossible.
    """
    spec = cfg.workbook
    problems: list[str] = []

    if wb.sheetnames[: len(spec.expected_sheets)] != spec.expected_sheets:
        problems.append(
            f"sheet names/order changed\n      expected: {spec.expected_sheets}\n"
            f"      found   : {wb.sheetnames}"
        )
    if spec.sheet not in wb.sheetnames:
        raise SchemaMismatchError(f"sheet {spec.sheet!r} is missing from the workbook")

    ws = wb[spec.sheet]
    last_col_index = column_index_from_string(spec.expected_last_col)
    if ws.max_column != last_col_index:
        problems.append(
            f"{spec.sheet} has {ws.max_column} columns, expected "
            f"{last_col_index} (A:{spec.expected_last_col})"
        )
    populated = last_data_row(ws, spec)
    if populated != spec.expected_last_row:
        problems.append(
            f"{spec.sheet} holds data to row {populated}, expected "
            f"{spec.expected_last_row} "
            f"({spec.expected_last_row - spec.first_data_row + 1} data rows)"
        )
    if ws.max_row > populated:
        # Tolerated: a spreadsheet app padded the used range. Say so rather than
        # failing, so the difference is visible if it ever means something else.
        log.info(
            "%s: used range runs to row %d but data ends at %d; "
            "%d trailing empty rows ignored",
            spec.sheet, ws.max_row, populated, ws.max_row - populated,
        )

    # Every mapped column must carry the header its logical name implies.
    header_by_letter = {
        get_column_letter(c): ws.cell(spec.header_row, c).value
        for c in range(1, ws.max_column + 1)
    }
    expected_headers = {
        "id": "ID",
        "entity_name": "Entity_Name",
        "category": "Category",
        "country": "Country",
        "website_url": "Website_URL",
        "general_email": "General_Email",
        "sales_b2b_email": "Sales_B2B_Email",
        "phone": "Phone",
        "whatsapp": "WhatsApp",
        "contact_person_name": "Contact_Person_Name",
        "contact_person_role": "Contact_Person_Role",
        "linkedin_url": "LinkedIn_URL",
        "instagram_handle": "Instagram_Handle",
        "commission_terms": "Commission_or_Partner_Terms",
        "contacted": "Contacted",
        "status": "Status",
        "source_url": "Source_URL",
        "date_verified": "Date_Verified",
        "round": "Round",
    }
    for logical, expected in expected_headers.items():
        letter = spec.column_for(logical)
        found = header_by_letter.get(letter)
        if found != expected:
            problems.append(
                f"column {letter} should be {expected!r} ({logical}) but holds {found!r}"
            )

    if problems:
        joined = "\n  - ".join(problems)
        raise SchemaMismatchError(
            "The workbook does not match config.yaml's schema:\n  - "
            + joined
            + "\n\nNothing has been changed. Fix config.yaml (or the workbook) and re-run."
        )


def count_formulas(wb: Workbook) -> int:
    return sum(
        1
        for ws in wb.worksheets
        for row in ws.iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith("=")
    )


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PartnerRow:
    """One PARTNERS data row as read, keyed by column letter."""

    row: int
    cells: dict[str, str | None]

    def get(self, letter: str) -> str:
        value = self.cells.get(letter)
        return "" if value is None else str(value).strip()

    def logical(self, cfg: Config, logical_field: str) -> str:
        return self.get(cfg.workbook.column_for(logical_field))


@dataclass(slots=True)
class WorkbookView:
    """Everything read from the workbook that the pipeline needs."""

    path: Path
    rows: list[PartnerRow]
    formula_count: int
    domain_row_counts: Counter[str] = field(default_factory=Counter)
    ledger_domains: dict[str, dict[str, str]] = field(default_factory=dict)
    duplicate_domains: dict[str, list[int]] = field(default_factory=dict)
    #: domain -> the Resort_Base of every row using it, for the scope guard.
    domain_resorts: dict[str, list[str]] = field(default_factory=dict)
    #: header text -> column letter, read from the sheet rather than configured,
    #: so the CRM columns can be shown by name without eleven config entries.
    header_letters: dict[str, str] = field(default_factory=dict)


def domain_of(url: str) -> str:
    """Registrable host for a website URL, lowercased, `www.` stripped.

    Returns an empty string for anything that is not a usable URL.
    """
    if not url:
        return ""
    text = url.strip()
    if not text or text.upper() == "TBD":
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlparse(text).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def normalise_website(url: str) -> str:
    """Canonical absolute URL for a Website_URL cell, or '' if unusable."""
    domain = domain_of(url)
    if not domain:
        return ""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    scheme = "https" if parsed.scheme not in ("http", "https") else parsed.scheme
    path = parsed.path or "/"
    return f"{scheme}://{parsed.netloc}{path}"


def _read_source_ledger(wb: Workbook) -> dict[str, dict[str, str]]:
    """Round-1 source ledger, keyed by domain.

    Used to log revisits. The `Exclude_Next_Round` flag governs *discovery*
    (Phase 2); enrichment revisits these domains on purpose, because Round 1 visited
    them to find entities, not to extract contacts.
    """
    if "_SOURCES" not in wb.sheetnames:
        return {}
    ws = wb["_SOURCES"]
    header_row = None
    for r in range(1, min(ws.max_row, 12) + 1):
        if str(ws.cell(r, 2).value or "").strip().lower() == "domain":
            header_row = r
            break
    if header_row is None:
        return {}

    headers = {
        str(ws.cell(header_row, c).value or "").strip(): c
        for c in range(1, ws.max_column + 1)
    }
    out: dict[str, dict[str, str]] = {}
    for r in range(header_row + 1, ws.max_row + 1):
        raw = ws.cell(r, headers.get("Domain", 2)).value
        if not raw:
            continue
        key = domain_of(str(raw)) or str(raw).strip().lower()
        out[key] = {
            "domain": key,
            "category_covered": str(ws.cell(r, headers.get("Category_Covered", 3)).value or ""),
            "round": str(ws.cell(r, headers.get("Round", 4)).value or ""),
            "exclude_next_round": str(
                ws.cell(r, headers.get("Exclude_Next_Round", 5)).value or ""
            ).strip().upper(),
        }
    return out


def load_workbook_view(cfg: Config, path: Path | None = None) -> WorkbookView:
    """Guard, open, validate and read the workbook."""
    target = path or cfg.workbook_file
    guard_readable(target, cfg.workbook.min_plausible_bytes)

    wb = load_workbook(target, data_only=False)
    try:
        assert_schema(wb, cfg)
        spec = cfg.workbook
        ws = wb[spec.sheet]
        letters = [get_column_letter(c) for c in range(1, ws.max_column + 1)]
        header_letters = {
            str(ws.cell(spec.header_row, c).value): letters[c - 1]
            for c in range(1, ws.max_column + 1)
            if ws.cell(spec.header_row, c).value
        }

        rows: list[PartnerRow] = []
        for r in range(spec.first_data_row, last_data_row(ws, spec) + 1):
            cells = {letters[c - 1]: ws.cell(r, c).value for c in range(1, ws.max_column + 1)}
            rows.append(PartnerRow(row=r, cells=cells))

        website_col = spec.column_for("website_url")
        resort_col = spec.column_for("resort_base")
        domain_counts: Counter[str] = Counter()
        by_domain: dict[str, list[int]] = defaultdict(list)
        resorts: dict[str, list[str]] = defaultdict(list)
        for pr in rows:
            d = domain_of(pr.get(website_col))
            if d:
                domain_counts[d] += 1
                by_domain[d].append(pr.row)
                resorts[d].append(pr.get(resort_col))

        return WorkbookView(
            path=target,
            rows=rows,
            formula_count=count_formulas(wb),
            domain_row_counts=domain_counts,
            ledger_domains=_read_source_ledger(wb),
            duplicate_domains={d: rs for d, rs in by_domain.items() if len(rs) > 1},
            domain_resorts=dict(resorts),
            header_letters=header_letters,
        )
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

NEEDS_MANUAL_URL_REASON = (
    "needs_manual_url: this domain refuses automated access (403/TLS), so the row "
    "needs a property-level Website_URL before it can be enriched"
)


def select_candidates(
    view: WorkbookView, cfg: Config
) -> tuple[list[Candidate], dict[int, str]]:
    """Split rows into what will be processed and what is skipped, with reasons.

    Skip reasons are reported verbatim in the run report, so a row never silently
    disappears from the pipeline.
    """
    spec = cfg.workbook
    candidates: list[Candidate] = []
    skipped: dict[int, str] = {}

    website_col = spec.column_for("website_url")
    name_col = spec.column_for("entity_name")
    id_col = spec.column_for("id")

    for pr in view.rows:
        skip_reason = ""
        for rule in cfg.selection.skip_when:
            current = pr.get(rule.column).upper()
            if current in {v.upper() for v in rule.values}:
                skip_reason = (
                    f"human-verified row ({rule.column}={pr.get(rule.column)!r}) "
                    "- never fetched, never written"
                )
                break
        if skip_reason:
            skipped[pr.row] = skip_reason
            continue

        website = pr.get(website_col)
        domain = domain_of(website)
        if cfg.selection.require_website and not domain:
            skipped[pr.row] = "no usable Website_URL - needs discovery, not enrichment"
            continue

        blocked = {d.lower() for d in cfg.scope.needs_manual_url}
        if domain in blocked or any(domain.endswith("." + d) for d in blocked):
            skipped[pr.row] = NEEDS_MANUAL_URL_REASON
            continue

        existing = {f: pr.logical(cfg, f) for f in ENRICHABLE_FIELDS}
        if all(not spec.is_empty(v) for v in existing.values()):
            skipped[pr.row] = "every enrichable field is already filled"
            continue

        candidates.append(
            Candidate(
                entity_id=pr.get(id_col) or f"ROW-{pr.row}",
                row=pr.row,
                name=pr.get(name_col),
                website_url=normalise_website(website),
                domain=domain_of(website),
                category=pr.logical(cfg, "category"),
                country=pr.logical(cfg, "country"),
                resort_base=pr.logical(cfg, "resort_base"),
                existing=existing,
            )
        )

    return candidates, skipped
