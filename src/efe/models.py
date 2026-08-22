"""Record models.

The shapes here are deliberately aligned with the Phase-1 SQLite schema in
`docs/ARCHITECTURE.md` §2. A `LedgerRecord` *is* a future `entity_field` row, so the
Phase-1 import is a straight replay of the JSONL ledger rather than a migration.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Confidence(StrEnum):
    """How trustworthy an extracted value is.

    Only `HIGH` is ever written to the workbook. Everything else goes to the review
    queue for a human to decide on.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class DataClass(StrEnum):
    """GDPR classification. Drives where a value is allowed to land."""

    CORPORATE_ROLE = "corporate_role"      # info@, sales@ - corporate data
    PERSONAL_NAMED = "personal_named"      # j.mueller@, a named individual
    UNKNOWN = "unknown"


class PageKind(StrEnum):
    """What kind of page a value came from. Feeds the confidence decision."""

    HOME = "home"
    CONTACT = "contact"
    IMPRESSUM = "impressum"
    TRADE = "trade"
    LEGAL = "legal"
    ABOUT = "about"
    TEAM = "team"
    SITEMAP = "sitemap"
    OTHER = "other"


class ScopeVerdict(StrEnum):
    """Whether a page may speak for the entity whose row we are filling."""

    OWN_DOMAIN = "own_domain"              # the entity's own site
    SHARED_MATCHED = "shared_matched"      # group domain, entity name matched
    SHARED_UNMATCHED = "shared_unmatched"  # group domain, no match -> never written


class Field_(StrEnum):
    """Logical field names. Map to workbook columns via config."""

    GENERAL_EMAIL = "general_email"
    SALES_B2B_EMAIL = "sales_b2b_email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    CONTACT_PERSON_NAME = "contact_person_name"
    CONTACT_PERSON_ROLE = "contact_person_role"
    LINKEDIN_URL = "linkedin_url"
    INSTAGRAM_HANDLE = "instagram_handle"
    COMMISSION_TERMS = "commission_terms"


class Evidence(BaseModel):
    """Proof that a value was published. Without all of this, nothing is written."""

    model_config = ConfigDict(frozen=True)

    source_url: str
    matched_text: str = Field(description="The raw substring as it appeared on the page")
    byte_offset: int = Field(
        ge=-1, description="Offset into the cached body; -1 if from a parsed attribute"
    )
    fetched_at: datetime
    page_kind: PageKind = PageKind.OTHER

    @field_validator("source_url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"evidence source_url must be an http(s) URL, got {v!r}")
        return v

    @field_validator("matched_text")
    @classmethod
    def _matched_text_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence matched_text may not be empty")
        return v


class ExtractedValue(BaseModel):
    """A single candidate value for a single field, with its provenance."""

    model_config = ConfigDict(frozen=True)

    field: Field_
    value: str
    confidence: Confidence
    data_class: DataClass = DataClass.UNKNOWN
    evidence: Evidence
    extractor: str = Field(description="Which extractor produced this, e.g. 'emails.mailto'")
    reason: str = Field(default="", description="Human-readable why, shown in dry-run output")
    scope: ScopeVerdict = ScopeVerdict.OWN_DOMAIN

    @field_validator("value")
    @classmethod
    def _value_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("extracted value may not be empty")
        return v

    @property
    def writable(self) -> bool:
        """Only high-confidence, in-scope, non-personal values may reach PARTNERS."""
        return (
            self.confidence is Confidence.HIGH
            and self.scope is not ScopeVerdict.SHARED_UNMATCHED
            and not (
                self.data_class is DataClass.PERSONAL_NAMED
                and self.field in (Field_.GENERAL_EMAIL, Field_.SALES_B2B_EMAIL)
            )
        )

    @property
    def held_back_reason(self) -> str:
        if self.writable:
            return ""
        if self.scope is ScopeVerdict.SHARED_UNMATCHED:
            return "group/chain domain, entity name not matched on the source page"
        if self.data_class is DataClass.PERSONAL_NAMED and self.field in (
            Field_.GENERAL_EMAIL,
            Field_.SALES_B2B_EMAIL,
        ):
            return "named-individual address (GDPR personal data) - never written to PARTNERS"
        return f"confidence={self.confidence.value}, below the write threshold"


class Candidate(BaseModel):
    """One PARTNERS row, reduced to what enrichment needs.

    Deliberately thin and workbook-free so a Phase-2 `Source` plugin can emit the
    same shape without importing anything from `efe.workbook`.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str                     # e.g. "EFE-0001"
    row: int                           # 1-based worksheet row
    name: str
    website_url: str
    domain: str
    category: str = ""
    country: str = ""
    resort_base: str = ""
    existing: dict[str, str] = Field(default_factory=dict)  # logical field -> current cell value


class CellChange(BaseModel):
    """One workbook cell that would be, or was, written."""

    row: int
    column: str                        # e.g. "I"
    field: str
    entity_id: str
    entity_name: str
    old_value: str
    new_value: str
    confidence: Confidence
    data_class: DataClass
    source_url: str
    fetched_at: datetime
    extractor: str
    note: str = ""


class LedgerRecord(BaseModel):
    """Append-only provenance record.

    Field-for-field the Phase-1 `entity_field` table:
        entity_field(entity_id, field, value, confidence, source_url, fetched_at, round_id)
    """

    entity_id: str
    field: str
    value: str
    confidence: str
    source_url: str
    fetched_at: datetime
    round_id: str
    # Phase-0 extras, ignored by a future SQLite import that selects explicit columns.
    data_class: str = DataClass.UNKNOWN.value
    written: bool = False
    held_back_reason: str = ""
    reason: str = ""
    extractor: str = ""
    matched_text: str = ""
    page_kind: str = PageKind.OTHER.value
    run_id: str = ""

    def to_jsonl(self) -> str:
        return self.model_dump_json()


class EntityResult(BaseModel):
    """Everything Phase 0 learned about one entity in one run."""

    candidate: Candidate
    values: list[ExtractedValue] = Field(default_factory=list)
    pages_fetched: list[str] = Field(default_factory=list)
    robots_blocked: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped_reason: str = ""
    revisited_ledger_domain: bool = False
    scope_verdict: ScopeVerdict = ScopeVerdict.OWN_DOMAIN
    shared_domain: bool = False
    shared_domain_reason: str = ""
    pages_matched: int = 0
    pages_unmatched: int = 0
    required_tokens: list[str] = Field(default_factory=list)
    shared_domain: bool = False
    shared_domain_reason: str = ""
    pages_matched: int = 0
    pages_unmatched: int = 0
    required_tokens: list[str] = Field(default_factory=list)
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.skipped_reason and not self.errors


class RunSummary(BaseModel):
    """The per-run report payload."""

    run_id: str
    round_id: str
    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = True
    workbook_in: str = ""
    workbook_out: str = ""
    rows_total: int = 0
    rows_selected: int = 0
    rows_processed: int = 0
    rows_skipped: dict[str, int] = Field(default_factory=dict)
    cells_written: dict[str, int] = Field(default_factory=dict)
    cells_still_tbd: dict[str, int] = Field(default_factory=dict)
    held_for_review: int = 0
    alternates_dropped: int = 0
    domains_abandoned: dict[str, str] = Field(default_factory=dict)
    pages_fetched: int = 0
    cache_hits: int = 0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    robots_blocked: list[str] = Field(default_factory=list)
    revisited_domains: list[dict[str, str]] = Field(default_factory=list)
    duplicate_entities: list[dict[str, Any]] = Field(default_factory=list)
    shared_domain_rows: list[dict[str, Any]] = Field(default_factory=list)
    needs_manual_url_rows: list[dict[str, str]] = Field(default_factory=list)


class WorkbookGuardError(RuntimeError):
    """Raised on a condition that must stop the run rather than be retried.

    Two environment failure modes get their own subclasses so the CLI can print the
    right instruction instead of a stack trace.
    """


class DriveSyncError(WorkbookGuardError):
    """The workbook read back as zero bytes, a placeholder, or a truncated file."""


class WorkbookLockedError(WorkbookGuardError):
    """The workbook is open in Excel and holds an exclusive lock."""


class SchemaMismatchError(WorkbookGuardError):
    """The workbook is readable but does not match the expected schema."""


class VerificationError(WorkbookGuardError):
    """The written output failed the fidelity gate. The output is discarded."""


def today_iso() -> str:
    """Date stamp in the workbook's own text format."""
    return date.today().isoformat()
