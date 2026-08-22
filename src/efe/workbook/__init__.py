"""Workbook I/O: guarded reading, guarded writing, and the fidelity gate.

Nothing in `efe.fetch` or `efe.extract` may import from this package. Keeping that
boundary is what lets a Phase-2 `Source` plugin reuse the fetcher unchanged.
"""

from efe.workbook.reader import (
    PartnerRow,
    WorkbookView,
    domain_of,
    guard_readable,
    guard_writable,
    load_workbook_view,
)
from efe.workbook.verify import FidelitySnapshot, compare, snapshot
from efe.workbook.writer import next_version_path, write_enriched

__all__ = [
    "FidelitySnapshot",
    "PartnerRow",
    "WorkbookView",
    "compare",
    "domain_of",
    "guard_readable",
    "guard_writable",
    "load_workbook_view",
    "next_version_path",
    "snapshot",
    "write_enriched",
]
