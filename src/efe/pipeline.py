"""Orchestration: fetch an entity's pages, extract, score, decide.

The run is resumable. Progress is kept in a JSONL ledger under `data/state/` whose
records are field-for-field the Phase-1 `entity_field` rows, so a crash at row 140
restarts at 140 and the same file later imports straight into SQLite. The workbook is
written once, at the very end, so a crash mid-crawl never leaves a half-written file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from efe.config import Config
from efe.extract import classify as classify_mod
from efe.extract import emails as emails_mod
from efe.extract import impressum as impressum_mod
from efe.extract import persons as persons_mod
from efe.extract import phones as phones_mod
from efe.extract import scope as scope_mod
from efe.extract import social as social_mod
from efe.extract import terms as terms_mod
from efe.extract.base import RawFind, strip_chrome
from efe.extract.scope import ScopeDecision, ScopeGuard
from efe.fetch.cache import CachedPage, PageCache
from efe.fetch.client import Fetcher
from efe.fetch.discovery import classify_page, contact_links, parse_sitemap, plan_urls
from efe.models import (
    Candidate,
    CellChange,
    Confidence,
    DataClass,
    EntityResult,
    Evidence,
    ExtractedValue,
    Field_,
    LedgerRecord,
    PageKind,
    ScopeVerdict,
)
from efe.workbook.reader import WorkbookView

log = logging.getLogger(__name__)

#: Logical field -> the config key holding its column letter.
FIELD_TO_COLUMN_KEY: dict[Field_, str] = {
    Field_.GENERAL_EMAIL: "general_email",
    Field_.SALES_B2B_EMAIL: "sales_b2b_email",
    Field_.PHONE: "phone",
    Field_.WHATSAPP: "whatsapp",
    Field_.CONTACT_PERSON_NAME: "contact_person_name",
    Field_.CONTACT_PERSON_ROLE: "contact_person_role",
    Field_.LINKEDIN_URL: "linkedin_url",
    Field_.INSTAGRAM_HANDLE: "instagram_handle",
    Field_.COMMISSION_TERMS: "commission_terms",
}


@dataclass(slots=True)
class FetchedPage:
    """One page plus the judgements that apply to everything extracted from it."""

    url: str
    kind: PageKind
    page: CachedPage
    scope: ScopeDecision

    @property
    def usable(self) -> bool:
        return self.page.ok and self.page.is_html and bool(self.page.body)


# ---------------------------------------------------------------------------
# Resume ledger
# ---------------------------------------------------------------------------

class RunLedger:
    """Append-only provenance ledger plus the resume marker."""

    def __init__(self, state_dir: Path, round_id: str, run_id: str) -> None:
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.round_id = round_id
        self.run_id = run_id
        self.path = self.dir / f"ledger-{round_id}.jsonl"
        self.progress_path = self.dir / f"progress-{round_id}.json"

    def completed_entities(self) -> set[str]:
        if not self.progress_path.is_file():
            return set()
        try:
            data = json.loads(self.progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return set(data.get("completed", []))

    def mark_complete(self, entity_ids: set[str], last_row: int | None = None) -> None:
        payload = {
            "round_id": self.round_id,
            "run_id": self.run_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_row": last_row,
            "completed": sorted(entity_ids),
        }
        tmp = self.progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.progress_path)

    def append(self, records: list[LedgerRecord]) -> None:
        if not records:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(record.to_jsonl() + "\n")

    def reset(self) -> None:
        self.progress_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

class Enricher:
    """Turns one `Candidate` into a set of `ExtractedValue`s with provenance."""

    def __init__(self, cfg: Config, fetcher: Fetcher, guard: ScopeGuard) -> None:
        self.cfg = cfg
        self.fetcher = fetcher
        self.guard = guard

    # -- page gathering -----------------------------------------------------
    async def _sitemap_urls(self, root: str) -> list[str]:
        if not self.cfg.discovery.use_sitemap:
            return []
        domain = urlparse(root).netloc
        seeds = [urljoin(root + "/", "sitemap.xml")]
        policy = self.fetcher.robots.get(
            domain[4:] if domain.startswith("www.") else domain
        )
        if policy is not None:
            seeds = list(dict.fromkeys(policy.sitemaps + seeds))

        collected: list[str] = []
        children_followed = 0
        queue = list(seeds[: 1 + self.cfg.discovery.max_sitemap_children])
        while queue and len(collected) < self.cfg.discovery.max_sitemap_urls:
            url = queue.pop(0)
            page = await self.fetcher.get(url)
            if not page.ok or not page.body:
                continue
            urls, children = parse_sitemap(page.body)
            collected.extend(urls)
            for child in children:
                if children_followed >= self.cfg.discovery.max_sitemap_children:
                    break
                children_followed += 1
                queue.append(child)
        return collected

    async def gather(self, candidate: Candidate) -> tuple[list[FetchedPage], list[str]]:
        """Fetch the homepage and the best contact-ish pages for one entity."""
        errors: list[str] = []
        parsed = urlparse(candidate.website_url)
        root = f"{parsed.scheme}://{parsed.netloc}"

        home = await self.fetcher.get(candidate.website_url)
        pages: list[FetchedPage] = []
        home_links: list[str] = []

        if home.ok and home.body:
            pages.append(self._wrap(candidate, candidate.website_url, home))
            home_links = contact_links(home.body, home.final_url or candidate.website_url,
                                       self.cfg.discovery)
        else:
            errors.append(f"homepage {candidate.website_url}: {home.error or home.status}")

        sitemap = await self._sitemap_urls(root)

        planned = plan_urls(
            candidate.website_url,
            candidate.domain,
            candidate.country,
            self.cfg.discovery,
            sitemap_urls=sitemap,
            page_links=home_links,
            limit=self.cfg.fetch.max_pages_per_entity,
        )

        budget = self.cfg.fetch.max_pages_per_entity
        fetched = 1 if pages else 0
        for url in planned:
            if fetched >= budget:
                break
            page = await self.fetcher.get(url)
            if page.error.startswith("not a usable http(s) URL"):
                # Never requested, so it costs no politeness and no budget.
                log.debug("skip malformed %s", url)
                continue
            fetched += 1
            if page.ok and page.body:
                pages.append(self._wrap(candidate, page.final_url or url, page))
            elif page.error and page.error not in ("", "robots.txt disallows this URL"):
                log.debug("skip %s: %s", url, page.error)
        return pages, errors

    def _wrap(self, candidate: Candidate, url: str, page: CachedPage) -> FetchedPage:
        kind = classify_page(url)
        if kind is not PageKind.IMPRESSUM and impressum_mod.looks_like_impressum(url, page.body):
            kind = PageKind.IMPRESSUM
        decision = self.guard.decide(
            entity_name=candidate.name,
            domain=candidate.domain,
            resort_base=candidate.resort_base,
            page_url=url,
            identity_text=identity_text(page.body),
            is_homepage=kind is PageKind.HOME,
        )
        return FetchedPage(url=url, kind=kind, page=page, scope=decision)

    # -- confidence ---------------------------------------------------------
    def _confidence(self, page: FetchedPage, *, field: Field_, method: str) -> Confidence:
        """Confidence for one value, from where it was found.

        `high` = an official contact, Impressum, legal or trade page of the entity's
        own site, scope guard satisfied. `medium` = anywhere else on the site.
        `low` = the scope guard rejected the page.
        """
        if page.scope.verdict is ScopeVerdict.SHARED_UNMATCHED:
            return Confidence.LOW

        conf_cfg = self.cfg.confidence
        high_kinds = {k.lower() for k in conf_cfg.high_page_kinds}

        if method.startswith("impressum-label"):
            return Confidence.HIGH
        if field in (Field_.LINKEDIN_URL, Field_.INSTAGRAM_HANDLE):
            if conf_cfg.social_on_own_domain_is_high:
                return Confidence.HIGH
        if page.kind.value in high_kinds:
            return Confidence.HIGH
        # The homepage promotion for role addresses lives in `_email_confidence`,
        # which is the only place that knows whether the address is on the entity's
        # own domain. Promoting here would also promote the web agency's address in
        # the same footer.
        return Confidence.MEDIUM

    def _email_confidence(
        self, page: FetchedPage, address: str, site_domain: str, base: Confidence
    ) -> tuple[Confidence, str]:
        """Downgrade an address whose domain is not the site that published it.

        A contact or Impressum page can carry a parent company's, a booking
        platform's or a web agency's address. Those are published, but they are not
        this partner's contact, so they are held for review rather than written.
        """
        if classify_mod.domain_matches_site(address, site_domain):
            if (
                base is not Confidence.HIGH
                and page.kind is PageKind.HOME
                and self.cfg.confidence.homepage_role_email_is_high
                and page.scope.verdict is not ScopeVerdict.SHARED_UNMATCHED
                # The promotion exists because a small operator's only role address
                # is often on their homepage. A group homepage is not that: its
                # central address belongs to the group, not to this property.
                and not self.guard.is_shared(site_domain)
            ):
                return Confidence.HIGH, (
                    "; published on the entity's own homepage, which for a small "
                    "operator is often the only place a role address appears"
                )
            return base, ""
        if classify_mod.is_freemail(address, self.cfg.email):
            # A gmail address is not on the entity's own domain, so it never gets the
            # homepage promotion -- only whatever the page kind already earned it.
            return base, (
                "; free-mail address published on the company's own site, "
                "which is common for small Alpine operators"
            )
        downgraded = min(base, Confidence.MEDIUM, key=lambda c: c.rank)
        return downgraded, (
            f"; address domain does not match the site domain ({site_domain}), "
            "so it is held for review rather than written"
        )

    def _evidence(self, page: FetchedPage, find: RawFind) -> Evidence:
        return Evidence(
            source_url=page.url,
            matched_text=find.matched_text or find.value,
            byte_offset=find.offset,
            fetched_at=page.page.fetched_at,
            page_kind=page.kind,
        )

    # -- extraction ---------------------------------------------------------
    def extract(self, candidate: Candidate, pages: list[FetchedPage]) -> list[ExtractedValue]:
        values: list[ExtractedValue] = []
        region = phones_mod.region_for(candidate.country, candidate.domain, self.cfg.phone)

        shared = self.guard.is_shared(candidate.domain)

        for page in pages:
            if not page.usable:
                continue
            body = page.page.body
            # On a group domain the header, nav and footer belong to the group, not
            # to this property. Contact details are taken from the page body only;
            # socials keep the full page because that is where they live, and are
            # gated by the handle-must-name-the-entity check instead.
            contact_body = strip_chrome(body) if shared else body

            if page.kind is PageKind.IMPRESSUM:
                values += self._from_impressum(candidate, page, contact_body, region)

            values += self._emails(candidate, page, contact_body, chrome_stripped=shared)
            values += self._phones(page, contact_body, region, chrome_stripped=shared)
            values += self._socials(candidate, page, body)
            values += self._persons(page, contact_body)
            values += self._terms(page, body)

        return values

    def _from_impressum(
        self, candidate: Candidate, page: FetchedPage, body: str, region: str | None
    ) -> list[ExtractedValue]:
        """Labelled Impressum fields: the strongest evidence available anywhere."""
        out: list[ExtractedValue] = []
        parsed = impressum_mod.extract_impressum(body, page.url)

        for find in parsed["email"]:
            routing = classify_mod.classify_email(
                find.value, page.kind, self.cfg.email, self.cfg.gdpr
            )
            if routing.field is None or not routing.writable:
                out.append(self._held_email(page, find, routing))
                continue
            base = (
                Confidence.HIGH
                if page.scope.verdict is not ScopeVerdict.SHARED_UNMATCHED
                else Confidence.LOW
            )
            confidence, note = self._email_confidence(
                page, find.value, candidate.domain, base
            )
            out.append(
                ExtractedValue(
                    field=routing.field,
                    value=find.value,
                    confidence=confidence,
                    data_class=routing.data_class,
                    evidence=self._evidence(page, find),
                    extractor=f"impressum.{find.method}",
                    reason=(
                        f"labelled 'E-Mail' on a legally mandated Impressum page; "
                        f"{routing.reason}{note}"
                    ),
                    scope=page.scope.verdict,
                )
            )

        for find in parsed["phone"]:
            normalised = phones_mod.to_e164(find.value, region, self.cfg.phone)
            if not normalised:
                continue
            out.append(
                ExtractedValue(
                    field=Field_.PHONE,
                    value=normalised,
                    confidence=self._confidence(page, field=Field_.PHONE, method=find.method),
                    data_class=DataClass.CORPORATE_ROLE,
                    evidence=self._evidence(page, find),
                    extractor=f"impressum.{find.method}",
                    reason=(
                        f"labelled 'Telefon' on a legally mandated Impressum page; "
                        f"normalised {find.value!r} -> {normalised} (region {region})"
                    ),
                    scope=page.scope.verdict,
                )
            )

        for find in parsed["person"]:
            role = find.extra.get("role", "")
            if not role:
                continue
            confidence = self._confidence(
                page, field=Field_.CONTACT_PERSON_NAME, method=find.method
            )
            reason = (
                f"Impressum names the legally required representative "
                f"({role}) -- GDPR personal data"
            )
            out.append(
                ExtractedValue(
                    field=Field_.CONTACT_PERSON_NAME,
                    value=find.value,
                    confidence=confidence,
                    data_class=DataClass.PERSONAL_NAMED,
                    evidence=self._evidence(page, find),
                    extractor=f"impressum.{find.method}",
                    reason=reason,
                    scope=page.scope.verdict,
                )
            )
            out.append(
                ExtractedValue(
                    field=Field_.CONTACT_PERSON_ROLE,
                    value=role,
                    confidence=confidence,
                    data_class=DataClass.PERSONAL_NAMED,
                    evidence=self._evidence(page, find),
                    extractor=f"impressum.{find.method}",
                    reason=f"role published alongside the name on the Impressum: {role}",
                    scope=page.scope.verdict,
                )
            )
        return out

    def _held_email(
        self, page: FetchedPage, find: RawFind, routing: classify_mod.EmailRouting
    ) -> ExtractedValue:
        """An address that will not be written, kept with its reason for review."""
        return ExtractedValue(
            field=routing.field or Field_.GENERAL_EMAIL,
            value=find.value,
            confidence=Confidence.MEDIUM
            if page.scope.verdict is not ScopeVerdict.SHARED_UNMATCHED
            else Confidence.LOW,
            data_class=routing.data_class,
            evidence=self._evidence(page, find),
            extractor=f"emails.{find.method}",
            reason=routing.reason,
            scope=page.scope.verdict,
        )

    def _emails(
        self, candidate: Candidate, page: FetchedPage, body: str,
        chrome_stripped: bool = False,
    ) -> list[ExtractedValue]:
        out: list[ExtractedValue] = []
        chrome_note = (
            "; taken from the page body only, with the group header/nav/footer removed"
            if chrome_stripped else ""
        )
        for find in emails_mod.extract_emails(body, self.cfg.email):
            routing = classify_mod.classify_email(
                find.value, page.kind, self.cfg.email, self.cfg.gdpr
            )
            if routing.field is None or not routing.writable:
                out.append(self._held_email(page, find, routing))
                continue

            base = self._confidence(page, field=routing.field, method=find.method)
            confidence, note = self._email_confidence(
                page, find.value, candidate.domain, base
            )
            reason = routing.reason + note
            out.append(
                ExtractedValue(
                    field=routing.field,
                    value=find.value,
                    confidence=confidence,
                    data_class=routing.data_class,
                    evidence=self._evidence(page, find),
                    extractor=f"emails.{find.method}",
                    reason=f"{reason}; found via {find.method} on a "
                           f"{page.kind.value} page{chrome_note}",
                    scope=page.scope.verdict,
                )
            )
        return out

    def _phones(
        self, page: FetchedPage, body: str, region: str | None,
        chrome_stripped: bool = False,
    ) -> list[ExtractedValue]:
        out: list[ExtractedValue] = []
        chrome_note = (
            "; page body only, group header/nav/footer removed" if chrome_stripped else ""
        )
        for find in phones_mod.extract_phones(body, region, self.cfg.phone):
            out.append(
                ExtractedValue(
                    field=Field_.PHONE,
                    value=find.value,
                    confidence=self._confidence(page, field=Field_.PHONE, method=find.method),
                    data_class=DataClass.CORPORATE_ROLE,
                    evidence=self._evidence(page, find),
                    extractor=f"phones.{find.method}",
                    reason=(
                        f"{find.method} on a {page.kind.value} page; "
                        f"{find.matched_text!r} normalised to E.164 (region {region})"
                        f"{chrome_note}"
                    ),
                    scope=page.scope.verdict,
                )
            )
        for find in phones_mod.extract_whatsapp(body, region, self.cfg.phone):
            out.append(
                ExtractedValue(
                    field=Field_.WHATSAPP,
                    value=find.value,
                    confidence=self._confidence(page, field=Field_.WHATSAPP, method=find.method),
                    data_class=DataClass.CORPORATE_ROLE,
                    evidence=self._evidence(page, find),
                    extractor=f"phones.{find.method}",
                    reason=(
                        "published as a WhatsApp contact "
                        f"({find.method}), never derived from the Phone value"
                    ),
                    scope=page.scope.verdict,
                )
            )
        return out

    def _social_confidence(
        self, candidate: Candidate, page: FetchedPage, field_name: Field_, handle: str,
        method: str,
    ) -> tuple[Confidence, str]:
        """On a group domain, the handle itself must name the entity.

        A chain footer links every sibling property's account. The page can pass the
        scope guard -- it really is the Val d'Isere contact page -- while the handles
        in its footer belong to Venice and Gordes.
        """
        base = self._confidence(page, field=field_name, method=method)
        if not self.guard.is_shared(candidate.domain):
            return base, ""

        tokens = self.guard.discriminating_tokens(candidate.name, candidate.domain)
        tokens += scope_mod.tokenise(candidate.resort_base, set())
        flat = re.sub(r"[^a-z0-9]", "", scope_mod.fold(handle))
        if any(token and token in flat for token in tokens):
            return base, f"; handle names the entity ({candidate.domain} is a group domain)"
        downgraded = min(base, Confidence.MEDIUM, key=lambda c: c.rank)
        return downgraded, (
            f"; {candidate.domain} is a group domain and this handle does not name "
            "this entity, so it may belong to a sibling property -- held for review"
        )

    def _socials(
        self, candidate: Candidate, page: FetchedPage, body: str
    ) -> list[ExtractedValue]:
        out: list[ExtractedValue] = []
        for find in social_mod.extract_linkedin(body, self.cfg.social):
            if find.extra.get("personal_profile"):
                continue
            confidence, note = self._social_confidence(
                candidate, page, Field_.LINKEDIN_URL, find.value, find.method
            )
            out.append(
                ExtractedValue(
                    field=Field_.LINKEDIN_URL,
                    value=find.value,
                    confidence=confidence,
                    data_class=DataClass.CORPORATE_ROLE,
                    evidence=self._evidence(page, find),
                    extractor="social.linkedin",
                    reason=f"company LinkedIn page linked from the site{note}",
                    scope=page.scope.verdict,
                )
            )
        for find in social_mod.extract_instagram(body, self.cfg.social):
            confidence, note = self._social_confidence(
                candidate, page, Field_.INSTAGRAM_HANDLE, find.value, find.method
            )
            out.append(
                ExtractedValue(
                    field=Field_.INSTAGRAM_HANDLE,
                    value=find.value,
                    confidence=confidence,
                    data_class=DataClass.CORPORATE_ROLE,
                    evidence=self._evidence(page, find),
                    extractor="social.instagram",
                    reason=f"Instagram profile linked from the site{note}",
                    scope=page.scope.verdict,
                )
            )
        return out

    #: Page kinds a named individual may be extracted from generically. LEGAL is
    #: excluded on purpose: `Proprietaire` on a mentions-legales page names the
    #: owning company, not a person, and produced `Leaders Club / Proprietaire`.
    PERSON_PAGE_KINDS = frozenset(
        {PageKind.CONTACT, PageKind.TEAM, PageKind.ABOUT, PageKind.HOME}
    )

    def _persons(self, page: FetchedPage, body: str) -> list[ExtractedValue]:
        if page.kind is PageKind.IMPRESSUM:
            return []  # already handled with much stronger labelled evidence
        if page.kind not in self.PERSON_PAGE_KINDS:
            return []
        out: list[ExtractedValue] = []
        for find in persons_mod.extract_persons(body)[:5]:
            role = find.extra.get("role", "")
            if not role:
                continue
            confidence = self._confidence(
                page, field=Field_.CONTACT_PERSON_NAME, method=find.method
            )
            out.append(
                ExtractedValue(
                    field=Field_.CONTACT_PERSON_NAME,
                    value=find.value,
                    confidence=confidence,
                    data_class=DataClass.PERSONAL_NAMED,
                    evidence=self._evidence(page, find),
                    extractor=f"persons.{find.method}",
                    reason=(
                        f"name and role published together on a {page.kind.value} page "
                        f"({role}) -- GDPR personal data"
                    ),
                    scope=page.scope.verdict,
                )
            )
            out.append(
                ExtractedValue(
                    field=Field_.CONTACT_PERSON_ROLE,
                    value=role,
                    confidence=confidence,
                    data_class=DataClass.PERSONAL_NAMED,
                    evidence=self._evidence(page, find),
                    extractor=f"persons.{find.method}",
                    reason=f"role published alongside {find.value!r}",
                    scope=page.scope.verdict,
                )
            )
        return out

    def _terms(self, page: FetchedPage, body: str) -> list[ExtractedValue]:
        finds = terms_mod.extract_terms(body, page.url, self.cfg.terms)
        if not finds:
            return []
        summary = terms_mod.summarise(finds, self.cfg.terms)
        if not summary:
            return []
        return [
            ExtractedValue(
                field=Field_.COMMISSION_TERMS,
                value=summary,
                confidence=self._confidence(
                    page, field=Field_.COMMISSION_TERMS, method=finds[0].method
                ),
                data_class=DataClass.CORPORATE_ROLE,
                evidence=self._evidence(page, finds[0]),
                extractor="terms.trade-page",
                reason=(
                    f"verbatim from the published trade/B2B page; signals: "
                    f"{finds[0].extra.get('signals', '')}"
                ),
                scope=page.scope.verdict,
            )
        ]


def identity_text(html_body: str) -> str:
    """What a page says it is about: title, og:title and headings.

    Deliberately not the body. A chain site's navigation lists every property the
    group owns, so matching against body text would let a group contact page claim
    to be about any one of them.
    """
    if not html_body:
        return ""
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover
        return ""

    parts: list[str] = []
    title = tree.css_first("title")
    if title is not None:
        parts.append(title.text(strip=True))
    for node in tree.css('meta[property="og:title"], meta[name="twitter:title"]'):
        content = node.attributes.get("content")
        if content:
            parts.append(content)
    for selector in ("h1", "h2", "h3"):
        for node in tree.css(selector)[:12]:
            parts.append(node.text(strip=True))
    return " | ".join(p for p in parts if p)[:2000]


# ---------------------------------------------------------------------------
# Choosing what to write
# ---------------------------------------------------------------------------

def _sort_key(value: ExtractedValue) -> tuple:
    """Best candidate first: confidence, then page kind, then extraction method."""
    kind_rank = {
        PageKind.IMPRESSUM: 0,
        PageKind.CONTACT: 1,
        PageKind.TRADE: 2,
        PageKind.LEGAL: 3,
        PageKind.TEAM: 4,
        PageKind.ABOUT: 5,
        PageKind.HOME: 6,
    }.get(value.evidence.page_kind, 9)
    method_rank = 0 if any(
        marker in value.extractor for marker in ("mailto", "tel-href", "impressum", "wa-link")
    ) else 1
    return (-value.confidence.rank, kind_rank, method_rank, len(value.value))


def choose_values(
    candidate: Candidate, values: list[ExtractedValue], cfg: Config
) -> tuple[dict[Field_, ExtractedValue], list[ExtractedValue], int]:
    """Split candidates into one winner per empty field, and everything held back.

    A field that already holds a real value in the workbook is never contested: all
    of its candidates go to the review queue instead.

    The same value is usually found on several pages -- a group site repeats every
    property's phone number in its footer -- so candidates are deduplicated by value
    (keeping the best-evidence occurrence) and then capped per field. The number
    dropped by the cap is returned so the report can state it rather than imply the
    queue was exhaustive.
    """
    spec = cfg.workbook
    by_field: dict[Field_, list[ExtractedValue]] = {}
    for value in values:
        by_field.setdefault(value.field, []).append(value)

    chosen: dict[Field_, ExtractedValue] = {}
    held: list[ExtractedValue] = []
    dropped = 0
    cap = cfg.review.max_alternates_per_field

    for field_name, group in by_field.items():
        group.sort(key=_sort_key)

        # One entry per distinct value; the sort above means the first occurrence
        # is the best-evidenced one.
        unique: dict[str, ExtractedValue] = {}
        for value in group:
            unique.setdefault(value.value.strip().lower(), value)
        group = list(unique.values())

        column_key = FIELD_TO_COLUMN_KEY[field_name]
        current = candidate.existing.get(column_key, "")
        cell_free = spec.is_empty(current)

        winner_taken = False
        kept_alternates = 0
        for value in group:
            if cell_free and not winner_taken and value.writable:
                chosen[field_name] = value
                winner_taken = True
            elif kept_alternates < cap:
                held.append(value)
                kept_alternates += 1
            else:
                dropped += 1

    # A person's name and role travel together or not at all.
    name = chosen.get(Field_.CONTACT_PERSON_NAME)
    role = chosen.get(Field_.CONTACT_PERSON_ROLE)
    if name and not role:
        held.append(chosen.pop(Field_.CONTACT_PERSON_NAME))
    elif role and not name:
        held.append(chosen.pop(Field_.CONTACT_PERSON_ROLE))
    elif name and role and name.evidence.source_url != role.evidence.source_url:
        held.append(chosen.pop(Field_.CONTACT_PERSON_ROLE))
        held.append(chosen.pop(Field_.CONTACT_PERSON_NAME))

    return chosen, held, dropped


def to_cell_change(
    candidate: Candidate, field_name: Field_, value: ExtractedValue, cfg: Config,
    *, written: bool
) -> CellChange:
    column_key = FIELD_TO_COLUMN_KEY[field_name]
    return CellChange(
        row=candidate.row,
        column=cfg.workbook.column_for(column_key),
        field=column_key,
        entity_id=candidate.entity_id,
        entity_name=candidate.name,
        old_value=candidate.existing.get(column_key, ""),
        new_value=value.value,
        confidence=value.confidence,
        data_class=value.data_class,
        source_url=value.evidence.source_url,
        fetched_at=value.evidence.fetched_at,
        extractor=value.extractor,
        note=value.reason if written else f"HELD FOR REVIEW - {value.held_back_reason}"
                                          f" | {value.reason}",
    )


def to_ledger_records(
    candidate: Candidate,
    chosen: dict[Field_, ExtractedValue],
    held: list[ExtractedValue],
    round_id: str,
    run_id: str,
) -> list[LedgerRecord]:
    """Provenance rows in Phase-1 `entity_field` shape."""
    records: list[LedgerRecord] = []
    for field_name, value in chosen.items():
        records.append(
            LedgerRecord(
                entity_id=candidate.entity_id,
                field=FIELD_TO_COLUMN_KEY[field_name],
                value=value.value,
                confidence=value.confidence.value,
                source_url=value.evidence.source_url,
                fetched_at=value.evidence.fetched_at,
                round_id=round_id,
                data_class=value.data_class.value,
                written=True,
                reason=value.reason,
                extractor=value.extractor,
                matched_text=value.evidence.matched_text[:300],
                page_kind=value.evidence.page_kind.value,
                run_id=run_id,
            )
        )
    for value in held:
        records.append(
            LedgerRecord(
                entity_id=candidate.entity_id,
                field=FIELD_TO_COLUMN_KEY[value.field],
                value=value.value,
                confidence=value.confidence.value,
                source_url=value.evidence.source_url,
                fetched_at=value.evidence.fetched_at,
                round_id=round_id,
                data_class=value.data_class.value,
                written=False,
                held_back_reason=value.held_back_reason or "not the best candidate for the cell",
                reason=value.reason,
                extractor=value.extractor,
                matched_text=value.evidence.matched_text[:300],
                page_kind=value.evidence.page_kind.value,
                run_id=run_id,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

@dataclass
class RunOutcome:
    results: list[EntityResult] = field(default_factory=list)
    changes: list[CellChange] = field(default_factory=list)
    held: list[CellChange] = field(default_factory=list)
    revisited: list[dict[str, str]] = field(default_factory=list)
    pages_fetched: int = 0
    cache_hits: int = 0
    robots_blocked: list[str] = field(default_factory=list)
    alternates_dropped: int = 0
    domains_abandoned: dict[str, str] = field(default_factory=dict)


async def run_enrichment(
    cfg: Config,
    view: WorkbookView,
    candidates: list[Candidate],
    *,
    round_id: str,
    run_id: str,
    use_cache: bool = True,
    ledger: RunLedger | None = None,
    on_progress=None,
    fetcher: Fetcher | None = None,
) -> RunOutcome:
    """Fetch, extract and decide for every candidate. Writes nothing to the workbook.

    Args:
        fetcher: an already-open `Fetcher` to use instead of opening one. Lets a
            caller share a session, and lets the test suite drive the whole pipeline
            through a stubbed transport with the cache accounting still correct.
    """
    guard = ScopeGuard(cfg.scope, view.domain_row_counts, view.domain_resorts)
    cfg.cache_directory.mkdir(parents=True, exist_ok=True)
    cache = fetcher.cache if fetcher is not None else PageCache(
        cfg.cache_directory, enabled=use_cache
    )

    outcome = RunOutcome()
    completed = ledger.completed_entities() if ledger else set()
    done: set[str] = set(completed)

    async with AsyncExitStack() as stack:
        fetcher = fetcher or await stack.enter_async_context(Fetcher(cfg.fetch, cache))
        enricher = Enricher(cfg, fetcher, guard)
        semaphore = asyncio.Semaphore(cfg.fetch.global_concurrency)

        async def one(candidate: Candidate) -> EntityResult:
            async with semaphore:
                result = EntityResult(candidate=candidate)
                ledger_entry = view.ledger_domains.get(candidate.domain)
                if ledger_entry:
                    result.revisited_ledger_domain = True
                    outcome.revisited.append(
                        {
                            "domain": candidate.domain,
                            "entity": candidate.name,
                            "round_1_purpose": ledger_entry.get("category_covered", ""),
                            "exclude_next_round": ledger_entry.get("exclude_next_round", ""),
                            "reason": (
                                "revisited on purpose: Round 1 visited this domain for "
                                "discovery, not contact extraction, which is why this "
                                "row is still TBD"
                            ),
                        }
                    )

                pages, errors = await enricher.gather(candidate)
                result.pages_fetched = [p.url for p in pages]
                result.errors = errors
                result.shared_domain = guard.is_shared(candidate.domain)
                if result.shared_domain:
                    result.required_tokens = guard.discriminating_tokens(
                        candidate.name, candidate.domain
                    )
                    result.shared_domain_reason = guard.shared_reason(candidate.domain)
                if pages and result.shared_domain:
                    # How many fetched pages actually identified this property. The
                    # domain-level reason stays as it is: mixing the two made the
                    # report contradict itself.
                    result.pages_matched = sum(
                        1 for p in pages
                        if p.scope.verdict is not ScopeVerdict.SHARED_UNMATCHED
                    )
                    result.pages_unmatched = len(pages) - result.pages_matched
                if pages:
                    result.scope_verdict = min(
                        (p.scope.verdict for p in pages),
                        key=lambda v: {"own_domain": 0, "shared_matched": 1,
                                       "shared_unmatched": 2}[v.value],
                    )
                result.values = enricher.extract(candidate, pages)
                result.finished_at = datetime.now()
                return result

        pending = [c for c in candidates if c.entity_id not in completed]
        for candidate in candidates:
            if candidate.entity_id in completed:
                log.info("resume: %s already done in round %s", candidate.entity_id, round_id)

        finished = 0
        lock = asyncio.Lock()

        async def one_and_record(candidate: Candidate) -> None:
            nonlocal finished
            result = await one(candidate)
            chosen, held, dropped = choose_values(candidate, result.values, cfg)
            async with lock:
                finished += 1
                outcome.alternates_dropped += dropped
                outcome.results.append(result)
                outcome.changes += [
                    to_cell_change(candidate, f, v, cfg, written=True)
                    for f, v in chosen.items()
                ]
                outcome.held += [
                    to_cell_change(candidate, v.field, v, cfg, written=False) for v in held
                ]
                if ledger:
                    ledger.append(
                        to_ledger_records(candidate, chosen, held, round_id, run_id)
                    )
                    done.add(candidate.entity_id)
                    ledger.mark_complete(done, last_row=candidate.row)
                if on_progress:
                    on_progress(finished, len(pending), candidate, chosen, held)

        await asyncio.gather(*(one_and_record(c) for c in pending))
        outcome.results.sort(key=lambda r: r.candidate.row)
        outcome.changes.sort(key=lambda c: (c.row, c.column))
        outcome.held.sort(key=lambda c: (c.row, c.column))

        outcome.pages_fetched = fetcher.requests_made
        outcome.robots_blocked = list(fetcher.robots.blocked_urls)
        outcome.domains_abandoned = dict(fetcher.skipped_domains)

    outcome.cache_hits = cache.hits
    return outcome
