"""Continuity state: what the last run read, so every regression has a baseline.

`data/state/workbook.json` records the file, its version, its data-row count and a
hash of its header after each successful load. The next load compares against it:
fewer rows, a lower version or a different header means an old file or a
half-synced one, and the run stops instead of quietly working on the wrong data.

The file is written atomically and read strictly: a baseline that exists but cannot
be parsed is an error, never "no baseline" -- that would switch the guard off at
exactly the moment (a crash mid-save) it is most needed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from efe.models import ContinuityError

STATE_FILENAME = "workbook.json"


@dataclass
class WorkbookState:
    file: str
    version: int
    data_rows: int
    header_sha256: str
    recorded_at: str
    command: str
    #: SHA-256 of the file itself, so 'which bytes did the last run read' is on record.
    file_sha256: str = ""

    @classmethod
    def now(
        cls,
        *,
        file: str,
        version: int,
        data_rows: int,
        header: list[str],
        command: str,
        file_sha256: str = "",
    ) -> WorkbookState:
        return cls(
            file=file,
            version=version,
            data_rows=data_rows,
            header_sha256=header_hash(header),
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            command=command,
            file_sha256=file_sha256,
        )

    @property
    def version_label(self) -> str:
        return f"v{self.version:02d}" if self.version else "unversioned"


def header_hash(header: list[str]) -> str:
    joined = "\x1f".join(str(h) for h in header)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def state_path(state_directory: Path) -> Path:
    return state_directory / STATE_FILENAME


def load_state(path: Path) -> WorkbookState | None:
    """The recorded state, or None when there is none (first run).

    Raises:
        ContinuityError: the file exists but cannot be read. Deleting it, or
            `--reset-state`, is a decision for a human, not a default.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = ("file", "version", "data_rows", "header_sha256", "recorded_at", "command")
        return WorkbookState(
            **{k: raw[k] for k in required}, file_sha256=str(raw.get("file_sha256", ""))
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ContinuityError(
            f"The continuity baseline exists but is unreadable:\n  {path}\n  ({exc})\n"
            "It records what the last run read, so a silent 'first run' here would "
            "switch the regression guard off. Inspect or delete the file, or re-run "
            "with --reset-state to accept the chosen workbook as the new baseline. "
            "Nothing has been changed."
        ) from exc


def save_state(path: Path, state: WorkbookState) -> None:
    """Write atomically: a crash mid-write must not leave a half baseline behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def continuity_notices(
    previous: WorkbookState | None,
    *,
    file: str,
    version: int,
    data_rows: int,
    file_sha256: str,
) -> list[str]:
    """Differences from the baseline that do not block a run but must not be silent.

    The audit of 2026-08-27 is what this exists for: v08 gained 66 hand-typed rows
    overnight under an unchanged version number, and every command accepted it
    without a word. Editing in Google Sheets is the sanctioned workflow, so this
    cannot be a refusal -- but a run that trusted the baseline's row count would be
    reasoning about a file that no longer exists.
    """
    if previous is None or not file_sha256 or not previous.file_sha256:
        return []
    if previous.file_sha256 == file_sha256:
        return []
    if previous.version and version and version != previous.version:
        return []  # a new version is expected to differ
    delta = data_rows - previous.data_rows
    change = f"{delta:+d}" if delta else "same count"
    return [
        f"{file} carries the baseline's version but different bytes:\n"
        f"      baseline  sha256 {previous.file_sha256[:12]}...  {previous.data_rows} data rows"
        f"   (recorded {previous.recorded_at}, {previous.command})\n"
        f"      this file sha256 {file_sha256[:12]}...  {data_rows} data rows   ({change})\n"
        "      The file was edited outside this tool since the baseline was recorded."
    ]


def continuity_problems(
    previous: WorkbookState | None,
    *,
    file: str,
    version: int,
    data_rows: int,
    header: list[str],
    file_sha256: str = "",
) -> list[str]:
    """Why the chosen workbook cannot follow the previous one. Empty == fine.

    Equal counts are fine (a re-run, or an output that the resolver picked up next
    time). Only going backwards is refused -- and a file with no version at all,
    read against a versioned baseline, is refused too rather than compared blind.
    """
    if previous is None:
        return []
    problems: list[str] = []
    if previous.version and not version:
        problems.append(
            f"this file carries no version ({file}); the baseline is "
            f"v{previous.version:02d} ({previous.file}), so it cannot be compared"
        )
    elif version and previous.version and version < previous.version:
        problems.append(
            f"version went backwards: last run read v{previous.version:02d} "
            f"({previous.file}), this file is v{version:02d} ({file})"
        )
    if data_rows < previous.data_rows:
        edited = (
            " and the bytes differ from the baseline, so this is an edit, not a stale sync"
            if file_sha256 and previous.file_sha256 and file_sha256 != previous.file_sha256
            else ""
        )
        problems.append(
            f"fewer data rows than last run: {previous.data_rows} in {previous.file}, "
            f"{data_rows} in {file} - an old file, or a sync that is not finished{edited}"
        )
    if header_hash(header) != previous.header_sha256:
        problems.append(
            f"the PARTNERS header differs from the baseline recorded for {previous.file} "
            "- the column contract changed since last run (the header itself already "
            "matched config.yaml). If that is intended, re-run with --reset-state to "
            "accept the new column layout as the baseline"
        )
    return problems
