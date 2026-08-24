"""Configuration loading.

Every tunable lives in `config.yaml`. Nothing in this package hardcodes a path, a
rate limit, a column letter or a keyword list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

# The repo ships `config.yaml`; `config.yml` is accepted as an alias because
# docs/ARCHITECTURE.md refers to it by that name.
CONFIG_FILENAMES = ("config.yaml", "config.yml")


class WorkbookColumns(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    entity_name: str
    category: str
    subcategory: str = "D"
    resort_base: str = "E"
    region_valley: str = "F"
    country: str
    website_url: str
    general_email: str
    sales_b2b_email: str
    phone: str
    whatsapp: str
    contact_person_name: str
    contact_person_role: str
    linkedin_url: str
    instagram_handle: str
    segment_tier: str = "Q"
    b2b_program_exists: str = "T"
    commission_terms: str
    priority_score: str = "Y"
    contacted: str
    status: str
    source_url: str
    date_verified: str
    round: str


class WorkbookConfig(BaseModel):
    sheet: str
    header_row: int
    first_data_row: int
    expected_last_row: int
    expected_last_col: str
    expected_sheets: list[str]
    expected_formula_count: int
    min_plausible_bytes: int = 50_000
    changelog_sheet: str
    changelog_detail_sheet: str
    tbd_token: str
    empty_tokens: list[str]
    columns: WorkbookColumns
    writable_columns: list[str]
    provenance_columns: list[str]
    crm_columns: list[str]
    formula_columns: list[str]

    def column_for(self, logical_field: str) -> str:
        return getattr(self.columns, logical_field)

    def is_empty(self, value: Any) -> bool:
        """True when a cell may be filled: it holds nothing meaningful yet."""
        if value is None:
            return True
        text = str(value).strip()
        return text.upper() in {t.strip().upper() for t in self.empty_tokens}


class SkipRule(BaseModel):
    column: str
    values: list[str]


class SelectionConfig(BaseModel):
    skip_when: list[SkipRule] = Field(default_factory=list)
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

    workbook_path: str
    output_dir: str
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
    def workbook_file(self) -> Path:
        return Path(self.workbook_path)

    @property
    def output_directory(self) -> Path:
        return Path(self.output_dir)

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
        overlap = set(wb.writable_columns) & set(wb.crm_columns)
        if overlap:
            problems.append(f"writable_columns overlaps crm_columns: {sorted(overlap)}")
        overlap = set(wb.provenance_columns) & set(wb.crm_columns)
        if overlap:
            problems.append(f"provenance_columns overlaps crm_columns: {sorted(overlap)}")
        overlap = set(wb.writable_columns) & set(wb.formula_columns)
        if overlap:
            problems.append(f"writable_columns overlaps formula_columns: {sorted(overlap)}")
        if wb.column_for("contacted") not in wb.crm_columns:
            problems.append("the Contacted column must be listed in crm_columns")
        try:
            if self.artifacts_directory == self.output_directory:
                problems.append(
                    "artifacts_dir must not be the same as output_dir: the Drive "
                    "folder receives the workbook only"
                )
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            pass
        for logical in ("general_email", "sales_b2b_email", "phone", "whatsapp",
                        "contact_person_name", "contact_person_role", "linkedin_url",
                        "instagram_handle", "commission_terms"):
            col = wb.column_for(logical)
            if col not in wb.writable_columns:
                problems.append(f"{logical} -> column {col} is not in writable_columns")
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
