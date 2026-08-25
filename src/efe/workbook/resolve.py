"""Which file is *the* workbook.

Nothing in config names a file. The Drive folder is scanned for
`<date>_<basename>_v<NN>.xlsx` -- the date with or without dashes, because Google
Sheets exports one way and humans type the other -- and the highest version wins,
newest mtime breaking a tie. Superseded copies, Excel lock files and sync
placeholders are excluded. Every decision is logged, so "which file did it read?"
is never a question, and zero candidates is a loud error that names the folder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from efe.models import DriveSyncError

log = logging.getLogger(__name__)

#: Excel's owner file: `Book.xlsx` open -> `~$Book.xlsx` beside it.
LOCK_PREFIX = "~$"


def filename_pattern(basename: str) -> re.Pattern[str]:
    """`YYYY-MM-DD_<basename>_vNN.xlsx` or `YYYYMMDD_<basename>_vNN.xlsx`.

    Month and day may be one digit (`2026-8-4_...`): a hand-typed name must still
    be read rather than silently skipped.
    """
    return re.compile(
        rf"^(?P<date>\d{{4}}-?\d{{1,2}}-?\d{{1,2}})_{re.escape(basename)}_v(?P<version>\d+)\.xlsx$",
        re.IGNORECASE,
    )


def version_tail(basename: str) -> re.Pattern[str]:
    """`..._<basename>_vNN.xlsx` with ANY prefix: what counts as "carrying a version".

    Looser than `filename_pattern` on purpose. A file the resolver would not read
    (odd date, extra prefix) must still block the writer from emitting its number.
    """
    return re.compile(rf"_{re.escape(basename)}_v(?P<version>\d+)\.xlsx$", re.IGNORECASE)


def parse_version(path: Path, basename: str) -> int | None:
    """The vNN of a workbook filename, or None when the name is not versioned."""
    match = version_tail(basename).search(path.name)
    return int(match.group("version")) if match else None


def normalise_date(raw: str) -> str:
    parts = raw.split("-") if "-" in raw else [raw[:4], raw[4:6], raw[6:]]
    year, month, day = (parts + ["", ""])[:3]
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


@dataclass(frozen=True)
class WorkbookCandidate:
    path: Path
    version: int
    date: str
    size: int
    mtime: float

    @property
    def mtime_iso(self) -> str:
        return datetime.fromtimestamp(self.mtime).isoformat(timespec="seconds")

    def describe(self) -> str:
        return (
            f"{self.path.name}  (v{self.version:02d}, {self.size:,} bytes, "
            f"modified {self.mtime_iso})"
        )


@dataclass
class Resolution:
    """What the resolver looked at and what it decided."""

    directory: Path
    pattern: str
    chosen: WorkbookCandidate | None
    #: (filename, reason) for everything in the folder that was not chosen.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    #: Explicit `--workbook` override, when the folder was not scanned.
    override: bool = False
    #: Further folders scanned (an `output_dir` that differs from `workbook_dir`).
    extra_directories: list[Path] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"workbook folder: {self.directory}"]
        for extra in self.extra_directories:
            lines.append(f"  also scanned: {extra}")
        if self.override and self.chosen:
            lines.append(f"  chosen  : {self.chosen.describe()}  [--workbook override]")
        elif self.chosen:
            lines.append(f"  chosen  : {self.chosen.describe()}")
        else:
            lines.append(f"  chosen  : NONE matched {self.pattern}")
        for name, reason in self.rejected:
            lines.append(f"  skipped : {name}  -- {reason}")
        return "\n".join(lines)


def candidate_for(path: Path, basename: str) -> WorkbookCandidate:
    """Describe one file as a candidate, whether or not its name is versioned.

    Used for the `--workbook` override: version is taken from the name when the
    name carries one, otherwise it is 0 -- the writer then refuses to derive an
    output version, and the continuity state is not updated from it.
    """
    match = filename_pattern(basename).match(path.name)
    stat = path.stat()
    return WorkbookCandidate(
        path=path,
        version=int(match.group("version")) if match else (parse_version(path, basename) or 0),
        date=normalise_date(match.group("date")) if match else "",
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


def _scan(
    directory: Path,
    *,
    pattern: re.Pattern[str],
    basename: str,
    min_bytes: int,
    tokens: list[str],
    exclude_tokens: tuple[str, ...] | list[str],
    candidates: list[WorkbookCandidate],
    rejected: list[tuple[str, str]],
) -> None:
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        name = entry.name
        if name.startswith(LOCK_PREFIX):
            rejected.append((name, "Excel lock file - the workbook is open somewhere"))
            continue
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".xlsx":
            rejected.append((name, "not an .xlsx file"))
            continue
        if any(tok in name.lower() for tok in tokens):
            rejected.append((name, "marked " + "/".join(exclude_tokens)))
            continue
        match = pattern.match(name)
        if not match:
            rejected.append((name, f"name does not match <date>_{basename}_vNN.xlsx"))
            continue
        size = entry.stat().st_size
        if size < min_bytes:
            rejected.append(
                (
                    name,
                    f"only {size:,} bytes, below min_plausible_bytes={min_bytes:,} "
                    "- a Drive placeholder or a download still in progress",
                )
            )
            continue
        candidates.append(
            WorkbookCandidate(
                path=entry,
                version=int(match.group("version")),
                date=normalise_date(match.group("date")),
                size=size,
                mtime=entry.stat().st_mtime,
            )
        )


def resolve_workbook(
    directory: Path,
    *,
    basename: str,
    min_bytes: int,
    exclude_tokens: tuple[str, ...] | list[str] = ("SUPERSEDED",),
    extra_directories: tuple[Path, ...] | list[Path] = (),
) -> Resolution:
    """Pick the workbook to read from `directory` (and `extra_directories`).

    Always logs the decision. `extra_directories` exists for a layout where the
    output folder differs from the input folder: the file the writer just emitted
    must still be the one read next.

    Raises:
        DriveSyncError: the folder is missing or holds no usable candidate.
    """
    if not directory.is_dir():
        raise DriveSyncError(
            f"Workbook folder not found:\n  {directory}\n"
            "If this is a Google Drive path, check the folder is synced offline "
            "(workbook_dir in config.yaml). Nothing has been changed."
        )

    pattern = filename_pattern(basename)
    tokens = [t.lower() for t in exclude_tokens]
    candidates: list[WorkbookCandidate] = []
    rejected: list[tuple[str, str]] = []
    extras = [d for d in extra_directories if d != directory and d.is_dir()]
    for folder in (directory, *extras):
        _scan(
            folder,
            pattern=pattern,
            basename=basename,
            min_bytes=min_bytes,
            tokens=tokens,
            exclude_tokens=exclude_tokens,
            candidates=candidates,
            rejected=rejected,
        )

    chosen = max(candidates, key=lambda c: (c.version, c.mtime)) if candidates else None
    for cand in sorted(candidates, key=lambda c: (-c.version, -c.mtime)):
        if cand is chosen:
            continue
        if chosen is not None and cand.version == chosen.version:
            reason = (
                f"same v{cand.version:02d} as the chosen file but older (modified {cand.mtime_iso})"
            )
        else:
            reason = f"v{cand.version:02d} is below the highest version"
        rejected.append((cand.path.name, reason))

    resolution = Resolution(
        directory=directory,
        pattern=pattern.pattern,
        chosen=chosen,
        rejected=rejected,
        extra_directories=extras,
    )
    log.info("%s", resolution.describe())

    if chosen is None:
        listing = "\n".join(f"    {name}  -- {why}" for name, why in rejected) or "    (empty)"
        raise DriveSyncError(
            f"No workbook found in\n  {directory}\n"
            f"Expected a file named <date>_{basename}_vNN.xlsx (date with or without "
            f"dashes) of at least {min_bytes:,} bytes. Folder contents:\n{listing}\n"
            "If the file is on Google Drive, let the sync finish, then re-run. "
            "Nothing has been changed."
        )
    return resolution


def highest_version_present(directories: list[Path], basename: str) -> tuple[int, Path | None]:
    """Highest vNN across `directories` among names ending in `_<basename>_vNN.xlsx`.

    Any prefix counts -- an odd date, a hand-typed tag -- and tiny placeholders count
    too; only lock files are ignored. The writer uses this to refuse an output
    version that already exists, whatever state that file is in: version numbers
    only ever go up. A copy retired with a `_SUPERSEDED` suffix no longer ends in
    `_vNN.xlsx`, so it neither gets read nor blocks re-emitting its number from the
    version before it.
    """
    tail = version_tail(basename)
    best, where = 0, None
    seen: set[Path] = set()
    for directory in directories:
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        for entry in directory.iterdir():
            if entry.name.startswith(LOCK_PREFIX) or not entry.is_file():
                continue
            match = tail.search(entry.name)
            if match and int(match.group("version")) > best:
                best, where = int(match.group("version")), entry
    return best, where
