"""Duplicate-row detection for PARTNERS.

The v03 recovery merge left several LATAM agencies in the sheet twice, distinguished
only by accents: `Matuete` / `Matueté`, `Julia Tours Mexico` / `Juliá Tours México`.
Comparing names after NFKD normalisation and stripping combining marks makes those
surface automatically.

**Nothing is ever merged.** Both rows may carry different CRM state — in three of the
six pairs one row is `Contacted = YES` with an email logged and the other is
untouched — so this module only reports, ranks and recommends. The decision is the
operator's, and `ARCHITECTURE.md` §3 says the same: a wrong merge silently destroys
two records, a missed merge just leaves a duplicate.

The hard part is not finding the duplicates, it is *not* flagging the deliberate
ones. `Air Zermatt` and `Air Zermatt (heli-ski division)` are two business units with
different contacts, as are `Cimalpes` / `Cimalpes (apartments division)`, the three
Scott Dunn rows, `Powder Byrne` / `Powder Byrne MICE` and `Heli Bernina` /
`Heli Bernina (heli-ski)`. Those are all *subset* matches whose extra tokens name a
business unit, so a subset match is only a duplicate when the extra tokens contain no
division marker.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from efe.config import Config
from efe.workbook.reader import PartnerRow, WorkbookView, domain_of

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

#: CRM columns, shown side by side so an operator can see what each row would lose.
CRM_FIELDS = (
    "Contacted", "Contact_Date", "Follow_Up_Days", "Next_Follow_Up", "Email_Sent",
    "Call_Made", "WhatsApp_Sent", "Meeting_Booked", "Agreement_Signed", "Status",
    "Next_Action",
)

#: Data columns counted when ranking which row is the fuller record.
DATA_FIELDS = (
    "entity_name", "category", "subcategory", "resort_base", "region_valley",
    "country", "website_url", "general_email", "sales_b2b_email", "phone", "whatsapp",
    "contact_person_name", "contact_person_role", "linkedin_url", "instagram_handle",
    "segment_tier", "commission_terms", "source_url",
)


def fold(text: str) -> str:
    """Accent-insensitive comparison form: `Matueté` and `Matuete` fold together."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def name_tokens(name: str, stopwords: set[str]) -> frozenset[str]:
    """Meaningful, accent-folded tokens of an entity name.

    Only legal-form suffixes are dropped. Dropping generic words too would merge
    genuinely different companies.
    """
    return frozenset(
        token
        for token in _TOKEN_SPLIT_RE.split(fold(name))
        if token and token not in stopwords
    )


@dataclass
class DuplicatePair:
    """Two rows that look like the same entity, with everything needed to decide."""

    row_a: int
    row_b: int
    name_a: str
    name_b: str
    domain_a: str
    domain_b: str
    relation: str                       # "identical name" | "one name extends the other"
    extra_tokens: list[str] = field(default_factory=list)
    filled_a: int = 0
    filled_b: int = 0
    crm_a: dict[str, str] = field(default_factory=dict)
    crm_b: dict[str, str] = field(default_factory=dict)

    @property
    def same_domain(self) -> bool:
        return bool(self.domain_a) and self.domain_a == self.domain_b

    #: Columns that are populated on every row regardless of outreach, so their
    #: presence says nothing. `Next_Follow_Up` is the live `=IFERROR(AA+AB,"")`
    #: formula and `Follow_Up_Days` is a default of 14.
    _NOT_CRM_STATE = ("Follow_Up_Days", "Next_Follow_Up")
    _EMPTY_VALUES = ("", "NO", "TBD", "NOT STARTED", "N/A", "-")

    @classmethod
    def _is_worked(cls, state: dict[str, str]) -> bool:
        """Whether a row shows real outreach history that a merge would destroy."""
        for key, value in state.items():
            if key in cls._NOT_CRM_STATE:
                continue
            text = str(value or "").strip()
            if not text or text.startswith("="):
                continue
            if text.upper() not in cls._EMPTY_VALUES:
                return True
        return False

    @property
    def crm_rows(self) -> tuple[bool, bool]:
        """Whether each row carries any CRM state a merge would destroy."""
        return self._is_worked(self.crm_a), self._is_worked(self.crm_b)

    @property
    def recommendation(self) -> tuple[str, str]:
        """(recommendation, why). Never an instruction to merge automatically."""
        crm_a, crm_b = self.crm_rows
        if crm_a and crm_b:
            return (
                "MERGE BY HAND",
                "both rows carry CRM state; deleting either loses outreach history",
            )
        if crm_a and not crm_b:
            return (
                f"keep {self.row_a}, drop {self.row_b}",
                f"row {self.row_a} has live CRM state ({self._crm_summary(self.crm_a)}); "
                f"row {self.row_b} has none",
            )
        if crm_b and not crm_a:
            return (
                f"keep {self.row_b}, drop {self.row_a}",
                f"row {self.row_b} has live CRM state ({self._crm_summary(self.crm_b)}); "
                f"row {self.row_a} has none",
            )
        if self.filled_a > self.filled_b:
            return (
                f"keep {self.row_a}, drop {self.row_b}",
                f"neither row has CRM state; row {self.row_a} is the fuller record "
                f"({self.filled_a} vs {self.filled_b} fields filled)",
            )
        if self.filled_b > self.filled_a:
            return (
                f"keep {self.row_b}, drop {self.row_a}",
                f"neither row has CRM state; row {self.row_b} is the fuller record "
                f"({self.filled_b} vs {self.filled_a} fields filled)",
            )
        return (
            f"keep {min(self.row_a, self.row_b)}, drop {max(self.row_a, self.row_b)}",
            "neither row has CRM state and both are equally complete; "
            "keeping the earlier row is arbitrary but harmless",
        )

    @classmethod
    def _crm_summary(cls, state: dict[str, str]) -> str:
        parts = [
            f"{k}={v}"
            for k, v in state.items()
            if k not in cls._NOT_CRM_STATE
            and str(v or "").strip()
            and not str(v).startswith("=")
            and str(v).strip().upper() not in cls._EMPTY_VALUES
        ]
        return ", ".join(parts) or "none"


def _filled_count(row: PartnerRow, cfg: Config) -> int:
    spec = cfg.workbook
    return sum(
        1
        for logical in DATA_FIELDS
        if not spec.is_empty(row.get(spec.column_for(logical)))
    )


def _crm_state(row: PartnerRow, header_to_letter: dict[str, str]) -> dict[str, str]:
    return {
        name: str(row.get(header_to_letter[name]) or "")
        for name in CRM_FIELDS
        if name in header_to_letter
    }


def find_duplicates(view: WorkbookView, cfg: Config) -> list[DuplicatePair]:
    """Rows that are the same entity entered twice.

    Two rows pair when their accent-folded token sets are equal, or when one is a
    subset of the other and the extra tokens name no business unit.
    """
    spec = cfg.workbook
    stopwords = {fold(w) for w in cfg.dedupe.name_stopwords}
    markers = {fold(w) for w in cfg.dedupe.division_markers}

    name_col = spec.column_for("entity_name")
    website_col = spec.column_for("website_url")
    # CRM columns are resolved from the sheet's own header row, so the report can
    # name all eleven without eleven more config entries.
    header_to_letter = dict(view.header_letters)

    rows = [pr for pr in view.rows if pr.get(name_col)]
    tokens = {pr.row: name_tokens(pr.get(name_col), stopwords) for pr in rows}

    pairs: list[DuplicatePair] = []
    for index, first in enumerate(rows):
        for second in rows[index + 1 :]:
            a, b = tokens[first.row], tokens[second.row]
            if not a or not b:
                continue

            if a == b:
                relation, extra = "identical name", []
            elif a < b or b < a:
                # One name contains the other. Two conditions, both necessary:
                #
                #   * the extra words must not name a business unit -- `Air Zermatt`
                #     vs `Air Zermatt (heli-ski division)` is two sets of contacts;
                #   * the rows must share a domain -- otherwise `Ski Travel` and
                #     `Alpino Ski Travel`, or `Travel Class` and `Royal Class
                #     Travel`, look like duplicates when they are separate companies.
                #
                # An exactly identical name is trusted across domains; a partial one
                # is not.
                extra = sorted(a ^ b)
                if any(token in markers for token in extra):
                    continue
                domain_a = domain_of(first.get(website_col))
                domain_b = domain_of(second.get(website_col))
                if not domain_a or domain_a != domain_b:
                    continue
                relation = "one name extends the other"
            else:
                continue

            pairs.append(
                _build_pair(first, second, relation, extra, cfg, header_to_letter,
                            name_col, website_col)
            )

    pairs.sort(key=lambda p: (p.row_a, p.row_b))
    return pairs


def _build_pair(
    first: PartnerRow, second: PartnerRow, relation: str, extra: list[str],
    cfg: Config, header_to_letter: dict[str, str], name_col: str, website_col: str,
) -> DuplicatePair:
    return DuplicatePair(
        row_a=first.row,
        row_b=second.row,
        name_a=first.get(name_col),
        name_b=second.get(name_col),
        domain_a=domain_of(first.get(website_col)),
        domain_b=domain_of(second.get(website_col)),
        relation=relation,
        extra_tokens=extra,
        filled_a=_filled_count(first, cfg),
        filled_b=_filled_count(second, cfg),
        crm_a=_crm_state(first, header_to_letter),
        crm_b=_crm_state(second, header_to_letter),
    )


def render_duplicates_report(
    pairs: list[DuplicatePair], view: WorkbookView, cfg: Config,
    generated_at: datetime | None = None,
) -> str:
    """A decision sheet: both rows side by side, and a recommendation per pair."""
    lines: list[str] = []
    add = lines.append
    stamp = (generated_at or datetime.now()).isoformat(timespec="seconds")

    add("# Duplicate PARTNERS rows")
    add("")
    add(f"**Generated:** {stamp}  ")
    add(f"**Workbook:** `{view.path}`  ")
    add(f"**Pairs found:** {len(pairs)}")
    add("")
    add(
        "> **Nothing here has been changed.** Both rows in a pair may carry different "
        "CRM state, so merging is a decision only you can make. This report exists to "
        "make that decision quick, not to make it for you."
    )
    add("")
    add(
        "Detection is accent-insensitive: names are NFKD-normalised and combining "
        "marks stripped, so `Matueté` and `Matuete` compare equal. Rows whose names "
        "differ by a business-unit word — `(heli-ski division)`, `(apartments "
        "division)`, `Kids Clubs`, `MICE` — are **not** listed: those are separate "
        "units with separate contacts, and the marker vocabulary is "
        "`dedupe.division_markers` in `config.yaml`."
    )
    add("")

    if not pairs:
        add("_No duplicate rows found._")
        return "\n".join(lines)

    add("## Summary")
    add("")
    add("| Pair | Rows | Entity | Match | Recommendation |")
    add("|---|---|---|---|---|")
    for pair in pairs:
        action, _ = pair.recommendation
        add(
            f"| {pairs.index(pair) + 1} | {pair.row_a} / {pair.row_b} "
            f"| {pair.name_a} | {pair.relation} | **{action}** |"
        )
    add("")

    for number, pair in enumerate(pairs, start=1):
        action, why = pair.recommendation
        add(f"## {number}. rows {pair.row_a} and {pair.row_b}")
        add("")
        add(f"| | Row {pair.row_a} | Row {pair.row_b} |")
        add("|---|---|---|")
        add(f"| **Entity_Name** | {pair.name_a} | {pair.name_b} |")
        add(f"| **Website** | `{pair.domain_a or '—'}` | `{pair.domain_b or '—'}` |")
        add(f"| **Fields filled** | {pair.filled_a} | {pair.filled_b} |")
        for crm_field in CRM_FIELDS:
            left = pair.crm_a.get(crm_field, "")
            right = pair.crm_b.get(crm_field, "")
            if not left and not right:
                continue
            if left.startswith("=") or right.startswith("="):
                # The same formula on two rows differs only by row number.
                add(f"| {crm_field} | _(live formula)_ | _(live formula)_ |")
                continue
            mark = " ⚠️" if left != right else ""
            add(f"| {crm_field} | {left or '—'} | {right or '—'}{mark} |")
        add("")
        add(f"- **Match:** {pair.relation}"
            + (f" (extra words: {', '.join(pair.extra_tokens)})" if pair.extra_tokens else ""))
        add(f"- **Same domain:** {'yes' if pair.same_domain else 'no — check carefully'}")
        add(f"- **Recommendation: {action}** — {why}")
        crm_a, crm_b = pair.crm_rows
        if crm_a and crm_b:
            add(
                "- ⚠️ Both rows have been worked. Copy the surviving row's missing "
                "fields across by hand before deleting anything."
            )
        add("")

    return "\n".join(lines)
