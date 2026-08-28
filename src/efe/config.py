"""Configuration loading.

Every tunable lives in `config.yaml`. Nothing in this package hardcodes a path, a
rate limit, a column letter or a keyword list.

The workbook section describes the *contract* -- the ordered column names, the
sheets that must exist, which columns may be written -- and never a particular
file: no filename, no row count, no formula count, no column letters. The file is
resolved from the Drive folder at run time (`efe.workbook.resolve`) and the letters
are derived from the header names, so a new version never needs a config edit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

# The repo ships `config.yaml`; `config.yml` is accepted as an alias because
# docs/ARCHITECTURE.md refers to it by that name.
CONFIG_FILENAMES = ("config.yaml", "config.yml")

#: Logical fields the code addresses by name. Every one must map to a header.
REQUIRED_LOGICAL_FIELDS = (
    "id",
    "entity_name",
    "category",
    "subcategory",
    "resort_base",
    "region_valley",
    "country",
    "website_url",
    "general_email",
    "sales_b2b_email",
    "phone",
    "whatsapp",
    "contact_person_name",
    "contact_person_role",
    "linkedin_url",
    "instagram_handle",
    "segment_tier",
    "b2b_program_exists",
    "commission_terms",
    "priority_score",
    "contacted",
    "status",
    "source_url",
    "date_verified",
    "round",
)

#: Logical fields the enricher fills; each must be a research or contact column.
#: `commission_terms` is deliberately absent: Commission_or_Partner_Terms is a
#: protected column -- a negotiated commission is Juan's to type, not ours to guess.
ENRICHABLE_LOGICAL_FIELDS = (
    "general_email",
    "sales_b2b_email",
    "phone",
    "whatsapp",
    "contact_person_name",
    "contact_person_role",
    "linkedin_url",
    "instagram_handle",
)


class SkipRule(BaseModel):
    #: Header name of the column (e.g. "Contacted").
    column: str
    values: list[str]


class WorkbookConfig(BaseModel):
    """The PARTNERS contract. Column *names* everywhere; letters are derived.

    Ownership is declared **per column, never per row**. Contacting a partner marks
    that row's CRM columns as Juan's -- it does not freeze the row: the moment a
    hotel answers is the moment its missing WhatsApp and LinkedIn matter most.
    """

    model_config = ConfigDict(extra="forbid")

    sheet: str
    header_row: int = 1
    first_data_row: int = 2
    #: Every PARTNERS column name, exactly and in order. This is the schema check.
    header: list[str]
    #: Sheets that must exist. Extra sheets and a different order are fine.
    required_sheets: list[str]
    #: Below this size a file is a Drive placeholder, not the workbook.
    min_plausible_bytes: int = 50_000
    #: A filename carrying any of these is never chosen by the resolver.
    exclude_name_tokens: list[str] = Field(default_factory=lambda: ["SUPERSEDED"])
    changelog_sheet: str
    changelog_detail_sheet: str
    tbd_token: str
    empty_tokens: list[str]
    #: Header name -> regex the cell must match to count as holding a real value.
    #: A `Sales_B2B_Email` holding a person's name is not an address, so it counts
    #: as empty and may be proposed again.
    value_patterns: dict[str, str] = Field(default_factory=dict)
    #: logical field -> header name.
    columns: dict[str, str]

    # -- ownership: the five lists partition the header, exactly ---------------
    #: Only `promote` creates these, on a brand-new row.
    identity_columns: list[str]
    #: The enricher may propose these, and only when the cell is empty.
    research_columns: list[str]
    #: Same rule, but a human value always wins: names and roles are people.
    contact_columns: list[str]
    #: Stamped or appended on rows that gained a value. Not "only if empty":
    #: Source_URL accumulates, Date_Verified and Round are re-stamped.
    provenance_columns: list[str]
    #: Never written or proposed by any tool, in any row. Juan's, end to end.
    protected_columns: list[str]
    #: Live formulas; must hold a formula on every data row. Subset of protected.
    formula_columns: list[str]
    #: Columns that feed live formulas (here or on other sheets). The writer
    #: preserves cached formula results, which a change to these would stale.
    formula_precedents: list[str] = Field(default_factory=list)
    #: Rows whose formula column may hold a typed value instead of the formula --
    #: `Contacted = YES` rows are the human's end to end. This is ONLY about the
    #: schema check; it grants no exemption from enrichment.
    formula_override_when: list[SkipRule] = Field(default_factory=list)

    _letters: dict[str, str] = PrivateAttr(default_factory=dict)

    # -- derived ownership views ---------------------------------------------
    @property
    def writable_columns(self) -> list[str]:
        """What the enricher may fill: research plus contact, in header order."""
        return [*self.research_columns, *self.contact_columns]

    @property
    def crm_columns(self) -> list[str]:
        """Kept name for the protected set; every caller means "hands off"."""
        return self.protected_columns

    # -- validation ---------------------------------------------------------
    @model_validator(mode="after")
    def _check_names(self) -> WorkbookConfig:
        problems: list[str] = []
        seen: set[str] = set()
        for name in self.header:
            if name in seen:
                problems.append(f"header lists {name!r} twice")
            seen.add(name)
        if not self.header:
            problems.append("header is empty")
        for logical in REQUIRED_LOGICAL_FIELDS:
            if logical not in self.columns:
                problems.append(f"columns is missing the logical field {logical!r}")
        for logical, name in self.columns.items():
            if name not in seen:
                problems.append(f"columns.{logical} -> {name!r} is not in header")
        owners = (
            ("identity_columns", self.identity_columns),
            ("research_columns", self.research_columns),
            ("contact_columns", self.contact_columns),
            ("provenance_columns", self.provenance_columns),
            ("protected_columns", self.protected_columns),
        )
        for label, names in (
            *owners,
            ("formula_columns", self.formula_columns),
            ("formula_precedents", self.formula_precedents),
            ("value_patterns", list(self.value_patterns)),
        ):
            for name in names:
                if name not in seen:
                    problems.append(f"{label} names {name!r}, which is not in header")

        # Every column has exactly one owner. This is the invariant that would have
        # caught Priority_Score and Round falling through the cracks when ownership
        # was first written down: a column nobody owns is a column anybody writes.
        owner_of: dict[str, list[str]] = {}
        for label, names in owners:
            for name in names:
                owner_of.setdefault(name, []).append(label)
        for name in self.header:
            claims = owner_of.get(name, [])
            if not claims:
                problems.append(
                    f"{name!r} belongs to no ownership list; add it to one of "
                    f"{[label for label, _ in owners]}"
                )
            elif len(claims) > 1:
                problems.append(f"{name!r} is claimed by {claims}; it must have one owner")

        for name in self.formula_columns:
            if name not in self.protected_columns:
                problems.append(f"formula column {name!r} must also be protected")
        for rule in self.formula_override_when:
            if rule.column not in seen:
                problems.append(
                    f"formula_override_when names {rule.column!r}, which is not in header"
                )
        if self.sheet not in self.required_sheets:
            problems.append(f"required_sheets must include the data sheet {self.sheet!r}")
        if problems:
            raise ValueError("workbook section: " + "; ".join(problems))
        self.bind_header(self.header)
        return self

    # -- letters, derived ----------------------------------------------------
    def bind_header(self, actual: list[str]) -> None:
        """Derive column letters from a header row (the contract, or the sheet's)."""
        self._letters = {
            str(name): get_column_letter(index)
            for index, name in enumerate(actual, start=1)
            if name not in (None, "")
        }

    def letter_of(self, header_name: str) -> str:
        try:
            return self._letters[header_name]
        except KeyError as exc:
            raise ValueError(
                f"column {header_name!r} is not in the bound header ({len(self._letters)} columns)"
            ) from exc

    def header_of(self, letter: str) -> str:
        for name, col in self._letters.items():
            if col == letter:
                return name
        return letter

    def column_for(self, logical_field: str) -> str:
        """Worksheet letter of a logical field, resolved through the header."""
        return self.letter_of(self.columns[logical_field])

    def letters(self, names: list[str]) -> list[str]:
        return [self.letter_of(n) for n in names]

    @property
    def writable_letters(self) -> list[str]:
        return self.letters(self.writable_columns)

    @property
    def provenance_letters(self) -> list[str]:
        return self.letters(self.provenance_columns)

    @property
    def crm_letters(self) -> list[str]:
        return self.letters(self.crm_columns)

    @property
    def protected_letters(self) -> list[str]:
        return self.letters(self.protected_columns)

    @property
    def identity_letters(self) -> list[str]:
        return self.letters(self.identity_columns)

    def owner_of(self, header_name: str) -> str:
        """Which ownership list a column belongs to, for a legible refusal."""
        for label, names in (
            ("identity", self.identity_columns),
            ("research", self.research_columns),
            ("contact", self.contact_columns),
            ("provenance", self.provenance_columns),
            ("protected", self.protected_columns),
        ):
            if header_name in names:
                return label
        return "unowned"

    @property
    def formula_letters(self) -> list[str]:
        return self.letters(self.formula_columns)

    @property
    def precedent_letters(self) -> list[str]:
        return self.letters(self.formula_precedents)

    @property
    def column_count(self) -> int:
        return len(self.header)

    def is_empty(self, value: Any, column: str | None = None) -> bool:
        """True when a cell may be filled: it holds nothing meaningful yet.

        With `column`, the column's own `value_patterns` regex also decides: a
        `Sales_B2B_Email` holding `Kai Schweigkofler - Travel Agency Support desk`
        is not an address, so it counts as empty and may be proposed again. The old
        value still travels with the proposal, so nothing is replaced unseen.
        """
        if value is None:
            return True
        text = str(value).strip()
        if text.upper() in {t.strip().upper() for t in self.empty_tokens}:
            return True
        pattern = self.value_patterns.get(column or "")
        return bool(pattern) and re.fullmatch(pattern, text) is None


class SelectionConfig(BaseModel):
    require_website: bool = True
    round_tag: str = "R2-enrich"
    #: Category prefixes ("1." matches "1. Hotels"). Empty = all categories.
    categories: list[str] = Field(default_factory=list)
    #: Resort names, matched accent/umlaut-insensitively against Resort_Base and
    #: Region_Valley. Empty = all resorts.
    resorts: list[str] = Field(default_factory=list)


class FetchConfig(BaseModel):
    user_agent: str
    contact_email: str
    timeout_seconds: float = 20
    connect_timeout_seconds: float = 10
    max_response_bytes: int = 2_097_152
    per_domain_delay_seconds: float = 2.0
    global_concurrency: int = 5
    max_pages_per_entity: int = 8
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    retry_on_status: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])
    respect_robots: bool = True
    robots_timeout_seconds: float = 10
    honour_crawl_delay: bool = True
    max_crawl_delay_seconds: float = 30
    max_consecutive_failures: int = 4
    verify_tls: bool = True
    follow_redirects: bool = True
    max_redirects: int = 5


class DiscoveryConfig(BaseModel):
    use_sitemap: bool = True
    max_sitemap_urls: int = 3000
    max_sitemap_children: int = 5
    path_candidates: list[str]
    language_prefixes: list[str]
    impressum_first_countries: list[str]
    impressum_first_tlds: list[str]
    impressum_paths: list[str]
    contact_url_tokens: list[str]
    contact_link_text: list[str]


class ScopeConfig(BaseModel):
    shared_domain_min_rows: int = 2
    chain_domains: list[str]
    needs_manual_url: list[str] = Field(default_factory=list)
    name_match_min_token_ratio: float = 0.5
    name_stopwords: list[str]


class EmailConfig(BaseModel):
    sales_local_parts: list[str]
    general_local_parts: list[str]
    reject_local_parts: list[str]
    reject_domains: list[str]
    freemail_domains: list[str]


class PhoneConfig(BaseModel):
    country_to_region: dict[str, str | None]
    tld_to_region: dict[str, str]
    min_digits: int = 7
    max_digits: int = 15


class DedupeConfig(BaseModel):
    name_stopwords: list[str] = Field(default_factory=list)
    division_markers: list[str] = Field(default_factory=list)


class ReviewConfig(BaseModel):
    max_alternates_per_field: int = 8


class SocialConfig(BaseModel):
    instagram_reject_paths: list[str]
    linkedin_company_paths: list[str]


class TermsConfig(BaseModel):
    source_path_tokens: list[str]
    max_chars: int = 300
    signal_patterns: list[str]


class ConfidenceConfig(BaseModel):
    write_threshold: str = "high"
    high_page_kinds: list[str]
    medium_page_kinds: list[str]
    social_on_own_domain_is_high: bool = True
    homepage_role_email_is_high: bool = False
    form_only_when_no_email: bool = True


class GdprConfig(BaseModel):
    write_personal_email_to_workbook: bool = False
    personal_name_min_tokens: int = 2
    personal_local_part_patterns: list[str]


class Config(BaseModel):
    """The whole of `config.yaml`, validated."""

    #: The Drive folder holding the versioned workbooks. Never a filename.
    workbook_dir: str
    #: Where the emitted workbook goes. Defaults to `workbook_dir`, so the next
    #: run's resolver picks the new version up without any config change.
    output_dir: str | None = None
    cache_dir: str
    state_dir: str = "./data/state"
    log_dir: str = "./data/logs"
    artifacts_dir: str = "./data/out"
    #: Deprecated alias for `artifacts_dir`, still honoured if a config sets it.
    dry_run_dir: str | None = None
    output_basename: str = "EFE_Alpine_Partner_Database"

    workbook: WorkbookConfig
    selection: SelectionConfig
    fetch: FetchConfig
    discovery: DiscoveryConfig
    scope: ScopeConfig
    email: EmailConfig
    phone: PhoneConfig
    social: SocialConfig
    terms: TermsConfig
    review: ReviewConfig = ReviewConfig()
    dedupe: DedupeConfig = DedupeConfig()
    confidence: ConfidenceConfig
    gdpr: GdprConfig

    # Populated by load(); not part of the YAML.
    config_path: Path = Field(default_factory=Path)
    repo_root: Path = Field(default_factory=Path)

    # -- resolved paths -----------------------------------------------------
    def _resolve(self, raw: str) -> Path:
        """Relative paths resolve against the repo root; absolute ones are kept."""
        p = Path(raw)
        return p if p.is_absolute() else (self.repo_root / p).resolve()

    @property
    def workbook_directory(self) -> Path:
        return self._resolve(self.workbook_dir)

    @property
    def output_directory(self) -> Path:
        return self._resolve(self.output_dir) if self.output_dir else self.workbook_directory

    @property
    def cache_directory(self) -> Path:
        return self._resolve(self.cache_dir)

    @property
    def state_directory(self) -> Path:
        return self._resolve(self.state_dir)

    @property
    def log_directory(self) -> Path:
        return self._resolve(self.log_dir)

    @property
    def artifacts_directory(self) -> Path:
        """Where every non-workbook output goes. Never the Drive folder."""
        return self._resolve(self.dry_run_dir or self.artifacts_dir)

    @property
    def dry_run_directory(self) -> Path:
        """Deprecated alias kept so older call sites keep working."""
        return self.artifacts_directory

    def sanity_check(self) -> list[str]:
        """Structural problems that would make a run unsafe. Empty list == fine."""
        problems: list[str] = []
        wb = self.workbook
        # The five ownership lists already partition the header (WorkbookConfig
        # validates that), so overlaps cannot happen. What is left to check is that
        # the cross-cutting lists land on columns nobody may write.
        writable = set(wb.writable_columns)
        overlap = writable & set(wb.formula_columns)
        if overlap:
            problems.append(f"a formula column may not be writable: {sorted(overlap)}")
        overlap = writable & set(wb.formula_precedents)
        if overlap:
            problems.append(f"a formula precedent may not be writable: {sorted(overlap)}")
        overlap = set(wb.provenance_columns) & set(wb.formula_precedents)
        if overlap:
            problems.append(f"a formula precedent may not be provenance: {sorted(overlap)}")
        if wb.columns["contacted"] not in wb.protected_columns:
            problems.append("the Contacted column must be protected")
        try:
            if self.artifacts_directory == self.output_directory:
                problems.append(
                    "artifacts_dir must not be the same as output_dir: the Drive "
                    "folder receives the workbook only"
                )
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            pass
        for logical in ENRICHABLE_LOGICAL_FIELDS:
            name = wb.columns[logical]
            if name not in wb.writable_columns:
                problems.append(
                    f"{logical} -> column {name!r} is {wb.owner_of(name)}, so the enricher "
                    "cannot fill it; move it to research/contact or drop it from "
                    "ENRICHABLE_LOGICAL_FIELDS"
                )
        return problems


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` looking for a directory holding a config file."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if any((directory / name).is_file() for name in CONFIG_FILENAMES):
            return directory
    return here


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration.

    Args:
        path: explicit config file. When omitted, `config.yaml` (or `config.yml`)
            is looked up from the current directory upward.
    """
    if path is not None:
        config_path = Path(path).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"config file not found: {config_path}")
        repo_root = config_path.parent
    else:
        repo_root = find_repo_root()
        for name in CONFIG_FILENAMES:
            if (repo_root / name).is_file():
                config_path = repo_root / name
                break
        else:  # pragma: no cover - find_repo_root guarantees one exists
            raise FileNotFoundError(
                f"no {' or '.join(CONFIG_FILENAMES)} found from {Path.cwd()} upward"
            )

    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = Config.model_validate({**raw, "config_path": config_path, "repo_root": repo_root})
    problems = cfg.sanity_check()
    if problems:
        joined = "\n  - ".join(problems)
        raise ValueError(f"{config_path} is internally inconsistent:\n  - {joined}")
    return cfg
