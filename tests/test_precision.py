"""Regressions for the false positives the first real dry run produced.

Each of these was written into the workbook by an earlier version of the extractors.
They are named after what actually went wrong.
"""

from __future__ import annotations

from collections import Counter

import pytest

from efe.extract.persons import extract_persons, looks_like_name
from efe.extract.scope import ScopeGuard
from efe.extract.social import extract_instagram
from efe.models import ScopeVerdict
from tests.conftest import fixture_text


def names(finds):
    return {f.value for f in finds}


# ---------------------------------------------------------------------------
# Persons: nothing from cookie banners, legal boilerplate or navigation
# ---------------------------------------------------------------------------

def test_no_person_is_extracted_from_boilerplate():
    """`Accept All / coo`, `French Data Protection Act / cco`, `Villars Palace / socia`."""
    assert extract_persons(fixture_text("person_false_positives.html")) == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Accept All",
        "Reject All",
        "Manage Settings",
        "French Data Protection Act",
        "Villars Palace",
        "Decouvrir Courchevel Les",
        "Head Concierge",
        "Our Team",
        "Book Now",
        "Grand Resort",
    ],
)
def test_boilerplate_is_not_a_name(phrase):
    assert looks_like_name(phrase) is False


@pytest.mark.parametrize(
    "phrase",
    ["Elena Vargas", "Thomas Brunner", "Katharina Meier", "Jean-Luc Martin",
     "Ana de Souza", "Nils Aaberg"],
)
def test_real_names_still_pass(phrase):
    assert looks_like_name(phrase) is True


def test_acronym_roles_are_case_sensitive_and_whole_words():
    """`coo` inside "Cookies" and `cco` inside "account" must not match."""
    body = (
        "<html><body>"
        "<div><h3>Anna Keller</h3><p>Cookies and accounts are managed here</p></div>"
        "</body></html>"
    )
    assert extract_persons(body) == []

    real = (
        "<html><body><div><h3>Anna Keller</h3><p>COO</p></div></body></html>"
    )
    finds = extract_persons(real)
    assert names(finds) == {"Anna Keller"}
    assert finds[0].extra["role"] == "COO"


def test_the_three_supported_shapes_all_work():
    finds = extract_persons(fixture_text("person_true_positives.html"))
    pairs = {f.value: f.extra["role"] for f in finds}

    assert pairs["Elena Vargas"] == "Director of Sales"       # adjacent elements
    assert pairs["Thomas Brunner"] == "General Manager"       # adjacent elements
    assert pairs["Sophie Laurent"] == "Reservations Manager"  # <br>-separated lines
    assert pairs["Katharina Meier"] == "Geschaeftsfuehrer"    # labelled line
    assert pairs["Marco Ricci"] == "Revenue Manager"          # separated line, dash
    assert pairs["Lukas Berger"] == "Marketing Manager"       # separated line, comma


def test_a_name_with_a_non_role_is_still_discarded():
    finds = extract_persons(fixture_text("person_true_positives.html"))
    assert "Nobody Here" not in names(finds)


def test_a_role_never_leaks_into_the_name():
    for find in extract_persons(fixture_text("person_true_positives.html")):
        assert find.extra["role"].lower() not in find.value.lower()
        assert len(find.value.split()) <= 4


# ---------------------------------------------------------------------------
# Socials on a group domain must name the entity
# ---------------------------------------------------------------------------

def test_group_footer_links_every_sibling_property(real_config):
    """The extractor finds them all; deciding which is ours is the scope guard's job."""
    handles = {f.value for f in extract_instagram(
        fixture_text("group_footer_socials.html"), real_config.social
    )}
    assert handles == {"@grandclassvenice", "@grandclassgordes", "@grandclassvaldisere"}


# ---------------------------------------------------------------------------
# The resort fallback must actually distinguish
# ---------------------------------------------------------------------------

@pytest.fixture
def barriere_guard(real_config):
    """Two rows on one group domain, both in the same resort."""
    return ScopeGuard(
        real_config.scope,
        Counter({"hotelsbarriere.example": 2}),
        {"hotelsbarriere.example": ["Courchevel", "Courchevel"]},
    )


def test_a_shared_resort_does_not_identify_a_sibling(barriere_guard):
    """Les Neiges must not take a contact off a Fouquet's page: both are Courchevel."""
    decision = barriere_guard.decide(
        entity_name="Hotel Barriere Les Neiges",
        domain="hotelsbarriere.example",
        resort_base="Courchevel",
        page_url="https://hotelsbarriere.example/collection-fouquet-s/courchevel/offres",
        identity_text="Fouquet's Courchevel - offres",
    )
    assert decision.verdict is ScopeVerdict.SHARED_UNMATCHED
    assert "same resort" in decision.reason


def test_the_sibling_whose_name_matches_is_still_accepted(barriere_guard):
    decision = barriere_guard.decide(
        entity_name="Fouquet's Courchevel",
        domain="hotelsbarriere.example",
        resort_base="Courchevel",
        page_url="https://hotelsbarriere.example/collection-fouquet-s/courchevel/offres",
        identity_text="Fouquet's Courchevel - offres",
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED


def test_an_unambiguous_resort_still_carries_a_match(real_config):
    guard = ScopeGuard(
        real_config.scope,
        Counter({"grandclass.example": 2}),
        {"grandclass.example": ["Megeve", "Verbier"]},
    )
    decision = guard.decide(
        entity_name="Grandclass Mountain Retreat",
        domain="grandclass.example",
        resort_base="Megeve",
        page_url="https://grandclass.example/destinations/megeve",
        identity_text="Megeve",
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED
    assert "no sibling row" in decision.reason


def test_resort_is_distinctive_helper(barriere_guard):
    assert barriere_guard.resort_is_distinctive("hotelsbarriere.example", "Courchevel") is False
    assert barriere_guard.resort_is_distinctive("hotelsbarriere.example", "Megeve") is True
    assert barriere_guard.resort_is_distinctive("hotelsbarriere.example", "") is False
    # A domain the guard knows nothing about is given the benefit of the doubt.
    assert barriere_guard.resort_is_distinctive("unknown.example", "Verbier") is True


def test_a_loyalty_programme_is_not_a_person():
    """`Leaders Club / Propriétaire` came off a mentions-légales page."""
    for phrase in ("Leaders Club", "Preferred Hotels", "Virtuoso Network",
                   "Relais Chateaux", "Signature Travel"):
        assert looks_like_name(phrase) is False


def test_persons_are_not_taken_from_legal_pages(real_config, tmp_path):
    """On a legal page `Propriétaire` names the owning company, not a person."""
    from datetime import datetime

    from efe.extract.scope import ScopeDecision, ScopeGuard
    from efe.fetch.cache import CachedPage, PageCache
    from efe.fetch.client import Fetcher
    from efe.models import PageKind
    from efe.pipeline import Enricher, FetchedPage

    body = (
        "<html><body><div><h3>Anna Keller</h3><p>Propriétaire</p></div></body></html>"
    )
    page_at = lambda kind: FetchedPage(  # noqa: E731
        url="https://x.example/mentions-legales",
        kind=kind,
        page=CachedPage(url="https://x.example/mentions-legales",
                        final_url="https://x.example/mentions-legales", status=200,
                        body=body, fetched_at=datetime(2026, 8, 21),
                        headers={"content-type": "text/html"}),
        scope=ScopeDecision(verdict=ScopeVerdict.OWN_DOMAIN, reason="own"),
    )
    enricher = Enricher(
        real_config,
        Fetcher(real_config.fetch, PageCache(tmp_path, enabled=False)),
        ScopeGuard(real_config.scope),
    )
    assert enricher._persons(page_at(PageKind.LEGAL), body) == []
    assert enricher._persons(page_at(PageKind.TEAM), body) != []


# ---------------------------------------------------------------------------
# Retina image references are not email addresses
# ---------------------------------------------------------------------------

def test_srcset_assets_are_not_extracted_as_emails(real_config):
    """`angela@2x.jpg` and friends came out of a srcset on a real lift-company site."""
    from efe.extract.emails import extract_emails

    body = (
        '<html><body>'
        '<img srcset="/img/angela@2x.jpg 2x, /img/angela@3x.webp 3x" src="/img/a.jpg">'
        '<img srcset="/img/clouds-center@2x.png 2x, /img/sally%20kim@2x.png 2x">'
        '<p>media--92aa2fb2--query@2x.webp</p>'
        '<p>Write to <a href="mailto:info@matterhornparadise.example">us</a></p>'
        '</body></html>'
    )
    found = {f.value for f in extract_emails(body, real_config.email)}
    assert found == {"info@matterhornparadise.example"}


# ---------------------------------------------------------------------------
# Trade-page detection matches path segments, not substrings
# ---------------------------------------------------------------------------

def test_provision_types_is_not_a_trade_page(real_config):
    """`pro` inside `provision-types` made a page about packed lunches a trade page."""
    from efe.extract.terms import is_trade_page

    assert is_trade_page(
        "https://x.example/travel-service/typically-tirolean/provision-types",
        real_config.terms,
    ) is False
    assert is_trade_page("https://x.example/promotions", real_config.terms) is False
    assert is_trade_page("https://x.example/products", real_config.terms) is False

    assert is_trade_page("https://x.example/pro", real_config.terms) is True
    assert is_trade_page("https://x.example/en/trade", real_config.terms) is True
    assert is_trade_page("https://x.example/travel-agents", real_config.terms) is True
    assert is_trade_page("https://x.example/b2b-partners", real_config.terms) is True


def test_english_provisions_is_not_a_commission_signal(real_config):
    """German `Provision` means commission; English `provisions` means supplies."""
    from efe.extract.terms import extract_terms

    body = (
        "<html><body><p>Whether it is a quick energy boost or an extensive snack on "
        "the mountain, everyone's hiking provisions are different.</p></body></html>"
    )
    assert extract_terms(body, "https://x.example/trade", real_config.terms) == []

    german = (
        "<html><body><p>Wir zahlen eine Provision von 10% an akkreditierte "
        "Reiseburos.</p></body></html>"
    )
    finds = extract_terms(german, "https://x.example/trade", real_config.terms)
    assert finds and "Provision" in finds[0].value


# ---------------------------------------------------------------------------
# Duplicate detection: accent-insensitive, and blind to business units
# ---------------------------------------------------------------------------

def _pairs(names_domains, real_config):
    """Build a throwaway WorkbookView from (row, name, domain) triples."""
    from efe.dedupe import find_duplicates
    from efe.workbook.reader import PartnerRow, WorkbookView

    spec = real_config.workbook
    rows = []
    for row, name, domain in names_domains:
        cells = {spec.column_for("entity_name"): name,
                 spec.column_for("website_url"): f"https://{domain}" if domain else "TBD"}
        rows.append(PartnerRow(row=row, cells=cells))
    view = WorkbookView(path=__import__("pathlib").Path("x.xlsx"), rows=rows,
                        formula_count=0, header_letters={})
    return {(p.row_a, p.row_b) for p in find_duplicates(view, real_config)}


def test_accent_variants_are_detected(real_config):
    found = _pairs([
        (1, "Matuete", "matuete.com"),
        (2, "Matueté", "matuete.com"),
        (3, "Julia Tours Mexico", "juliatours.com.mx"),
        (4, "Juliá Tours México", "juliatours.com.mx"),
        (5, "Viajes Bojorquez (Matriz)", "viajesbojorquez.com"),
        (6, "Viajes Bojórquez (Matriz)", "viajesbojorquez.com"),
    ], real_config)
    assert found == {(1, 2), (3, 4), (5, 6)}


def test_word_order_and_repetition_do_not_hide_a_duplicate(real_config):
    found = _pairs([
        (1, "NUBA Travel", "nuba.com"),
        (2, "NUBA (Nuba Travel)", "nuba.com"),
        (3, "TM Travel Tailor Made", "tmtravel.com.br"),
        (4, "TM Travel Tailor Made Travel", "tmtravel.com.br"),
    ], real_config)
    assert found == {(1, 2), (3, 4)}


def test_an_expanded_name_on_the_same_domain_is_a_duplicate(real_config):
    assert _pairs([
        (1, "TTW Group (Travel Trend Worldwide)", "ttwgroup.com"),
        (2, "TTW Group", "ttwgroup.com"),
    ], real_config) == {(1, 2)}


def test_business_units_are_never_flagged(real_config):
    """Different divisions have different contacts. Merging them destroys both."""
    assert _pairs([
        (1, "Air Zermatt", "air-zermatt.ch"),
        (2, "Air Zermatt (heli-ski division)", "air-zermatt.ch"),
        (3, "Cimalpes", "cimalpes.com"),
        (4, "Cimalpes (apartments division)", "cimalpes.com"),
        (5, "Scott Dunn", "scottdunn.com"),
        (6, "Scott Dunn (chalet division)", "scottdunn.com"),
        (7, "Scott Dunn Kids Clubs / Childcare", "scottdunn.com"),
        (8, "Powder Byrne", "powderbyrne.com"),
        (9, "Powder Byrne MICE", "powderbyrne.com"),
        (10, "Heli Bernina", "helibernina.ch"),
        (11, "Heli Bernina (heli-ski)", "helibernina.ch"),
        (12, "Six Senses Residences Courchevel", "sixsenses.com"),
        (13, "Six Senses Crans-Montana", "sixsenses.com"),
    ], real_config) == set()


def test_different_companies_sharing_words_are_not_duplicates(real_config):
    """`Ski Travel` and `Alpino Ski Travel` are separate agencies on separate sites."""
    assert _pairs([
        (1, "Ski Travel", "skitravel.example"),
        (2, "Alpino Ski Travel", "alpinoski.example"),
        (3, "Ski Explore Travel", "skiexplore.example"),
        (4, "Travel Class", "travelclass.example"),
        (5, "Royal Class Travel", "royalclass.example"),
    ], real_config) == set()


def test_recommendation_protects_the_row_with_crm_state(real_config):
    from efe.dedupe import DuplicatePair

    pair = DuplicatePair(
        row_a=170, row_b=235, name_a="Matuete", name_b="Matueté",
        domain_a="matuete.com", domain_b="matuete.com", relation="identical name",
        filled_a=12, filled_b=11,
        crm_a={"Contacted": "YES", "Status": "Contacted", "Email_Sent": "X",
               "Next_Follow_Up": '=IFERROR(AA170+AB170,"")', "Follow_Up_Days": "14"},
        crm_b={"Contacted": "NO", "Status": "Not started", "Email_Sent": "",
               "Next_Follow_Up": '=IFERROR(AA235+AB235,"")', "Follow_Up_Days": "14"},
    )
    action, why = pair.recommendation
    assert action == "keep 170, drop 235"
    assert "live CRM state" in why
    assert pair.crm_rows == (True, False)


def test_a_row_with_only_defaults_is_not_treated_as_worked(real_config):
    """`Next_Follow_Up` is a live formula on every row and `Follow_Up_Days` is 14."""
    from efe.dedupe import DuplicatePair

    untouched = {"Contacted": "NO", "Status": "Not started", "Next_Action": "TBD",
                 "Next_Follow_Up": '=IFERROR(AA9+AB9,"")', "Follow_Up_Days": "14"}
    assert DuplicatePair._is_worked(untouched) is False


def test_both_worked_rows_are_never_auto_resolved(real_config):
    from efe.dedupe import DuplicatePair

    worked = {"Contacted": "YES", "Status": "Contacted"}
    pair = DuplicatePair(
        row_a=1, row_b=2, name_a="A", name_b="A", domain_a="x.example",
        domain_b="x.example", relation="identical name",
        crm_a=dict(worked), crm_b=dict(worked),
    )
    action, why = pair.recommendation
    assert action == "MERGE BY HAND"
    assert "loses outreach history" in why


# ---------------------------------------------------------------------------
# Homepage role addresses are high -- but only on the entity's own domain
# ---------------------------------------------------------------------------

def _enricher(real_config, tmp_path):
    from efe.extract.scope import ScopeGuard
    from efe.fetch.cache import PageCache
    from efe.fetch.client import Fetcher
    from efe.pipeline import Enricher

    return Enricher(
        real_config,
        Fetcher(real_config.fetch, PageCache(tmp_path, enabled=False)),
        ScopeGuard(real_config.scope),
    )


def _home_page(url="https://alpina.example/"):
    from datetime import datetime

    from efe.extract.scope import ScopeDecision
    from efe.fetch.cache import CachedPage
    from efe.models import PageKind
    from efe.pipeline import FetchedPage

    return FetchedPage(
        url=url,
        kind=PageKind.HOME,
        page=CachedPage(url=url, final_url=url, status=200, body="<html></html>",
                        fetched_at=datetime(2026, 8, 21),
                        headers={"content-type": "text/html"}),
        scope=ScopeDecision(verdict=ScopeVerdict.OWN_DOMAIN, reason="own domain"),
    )


def test_own_domain_homepage_role_address_is_high(real_config, tmp_path):
    from efe.models import Confidence, Field_

    assert real_config.confidence.homepage_role_email_is_high is True
    enricher = _enricher(real_config, tmp_path)
    base = enricher._confidence(_home_page(), field=Field_.GENERAL_EMAIL, method="mailto")
    confidence, note = enricher._email_confidence(
        _home_page(), "info@alpina.example", "alpina.example", base
    )
    assert confidence is Confidence.HIGH
    assert "own homepage" in note


def test_a_third_party_address_in_the_same_footer_stays_held(real_config, tmp_path):
    """The web agency, the PR firm and the booking platform are not this partner."""
    from efe.models import Confidence, Field_

    enricher = _enricher(real_config, tmp_path)
    base = enricher._confidence(_home_page(), field=Field_.GENERAL_EMAIL, method="mailto")
    for foreign in ("contact@webagency.example", "hello@pr-firm.example",
                    "support@bookingplatform.example"):
        confidence, note = enricher._email_confidence(
            _home_page(), foreign, "alpina.example", base
        )
        assert confidence is Confidence.MEDIUM, foreign
        assert "does not match the site domain" in note


def test_a_freemail_address_never_gets_the_homepage_promotion(real_config, tmp_path):
    from efe.models import Confidence, Field_

    enricher = _enricher(real_config, tmp_path)
    base = enricher._confidence(_home_page(), field=Field_.GENERAL_EMAIL, method="mailto")
    confidence, _ = enricher._email_confidence(
        _home_page(), "alpina.hotel@gmail.com", "alpina.example", base
    )
    assert confidence is Confidence.MEDIUM


def test_the_promotion_does_not_apply_on_an_unmatched_group_page(real_config, tmp_path):
    """A group homepage is not this property's homepage."""
    from datetime import datetime

    from efe.extract.scope import ScopeDecision
    from efe.fetch.cache import CachedPage
    from efe.models import Confidence, Field_, PageKind
    from efe.pipeline import FetchedPage

    page = FetchedPage(
        url="https://grandclass.example/",
        kind=PageKind.HOME,
        page=CachedPage(url="https://grandclass.example/",
                        final_url="https://grandclass.example/", status=200,
                        body="<html></html>", fetched_at=datetime(2026, 8, 21),
                        headers={"content-type": "text/html"}),
        scope=ScopeDecision(verdict=ScopeVerdict.SHARED_UNMATCHED, reason="group page"),
    )
    enricher = _enricher(real_config, tmp_path)
    base = enricher._confidence(page, field=Field_.GENERAL_EMAIL, method="mailto")
    confidence, _ = enricher._email_confidence(
        page, "info@grandclass.example", "grandclass.example", base
    )
    assert confidence is Confidence.LOW


# ---------------------------------------------------------------------------
# Rows on domains that refuse automated access are never fetched
# ---------------------------------------------------------------------------

def test_needs_manual_url_rows_are_skipped_without_a_fetch(synthetic_config):
    from efe.workbook.reader import load_workbook_view, select_candidates

    synthetic_config.scope.needs_manual_url = ["summitlodge.example"]
    view = load_workbook_view(synthetic_config)
    candidates, skipped = select_candidates(view, synthetic_config)

    assert not any(c.domain == "summitlodge.example" for c in candidates)
    reasons = [r for r in skipped.values() if r.startswith("needs_manual_url")]
    assert reasons
    assert "property-level Website_URL" in reasons[0]


def test_needs_manual_url_matches_subdomains(synthetic_config):
    from efe.workbook.reader import load_workbook_view, select_candidates

    synthetic_config.scope.needs_manual_url = ["example"]
    view = load_workbook_view(synthetic_config)
    candidates, _ = select_candidates(view, synthetic_config)
    assert candidates == []


def test_a_group_homepage_cannot_identify_one_property(real_config):
    """`airelles.com/` lists Courchevel, Val d'Isere and Gordes in its headings."""
    guard = ScopeGuard(real_config.scope, Counter({"airelles.example": 2}))
    identity = "Airelles | Courchevel · Val d'Isere · Gordes · Saint-Tropez"

    home = guard.decide(
        entity_name="Airelles Courchevel - Les Airelles",
        domain="airelles.example", resort_base="Courchevel 1850",
        page_url="https://airelles.example/", identity_text=identity,
        is_homepage=True,
    )
    assert home.verdict is ScopeVerdict.SHARED_UNMATCHED
    assert "group homepage" in home.reason

    # The same words on a property page are a genuine match.
    inner = guard.decide(
        entity_name="Airelles Courchevel - Les Airelles",
        domain="airelles.example", resort_base="Courchevel 1850",
        page_url="https://airelles.example/fr/destination/courchevel/contacts",
        identity_text="Les Airelles Courchevel - contacts", is_homepage=False,
    )
    assert inner.verdict is ScopeVerdict.SHARED_MATCHED


def test_the_group_row_itself_still_accepts_its_homepage(real_config):
    guard = ScopeGuard(real_config.scope, Counter({"airelles.example": 2}))
    decision = guard.decide(
        entity_name="Airelles", domain="airelles.example", resort_base="",
        page_url="https://airelles.example/", identity_text="Airelles",
        is_homepage=True,
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED
    assert "the group itself" in decision.reason


def test_no_homepage_promotion_on_a_group_domain(real_config, tmp_path):
    """`info@airelles.com` is the group's address, not the Courchevel property's."""
    from datetime import datetime

    from efe.extract.scope import ScopeDecision
    from efe.fetch.cache import CachedPage
    from efe.models import Confidence, Field_, PageKind
    from efe.pipeline import FetchedPage

    guard = ScopeGuard(real_config.scope, Counter({"airelles.com": 2}))
    from efe.fetch.cache import PageCache
    from efe.fetch.client import Fetcher
    from efe.pipeline import Enricher

    enricher = Enricher(
        real_config, Fetcher(real_config.fetch, PageCache(tmp_path, enabled=False)), guard
    )
    page = FetchedPage(
        url="https://airelles.com/", kind=PageKind.HOME,
        page=CachedPage(url="https://airelles.com/", final_url="https://airelles.com/",
                        status=200, body="<html></html>",
                        fetched_at=datetime(2026, 8, 21),
                        headers={"content-type": "text/html"}),
        scope=ScopeDecision(verdict=ScopeVerdict.SHARED_MATCHED, reason="matched"),
    )
    base = enricher._confidence(page, field=Field_.GENERAL_EMAIL, method="mailto")
    confidence, _ = enricher._email_confidence(
        page, "info@airelles.com", "airelles.com", base
    )
    assert confidence is Confidence.MEDIUM


# ---------------------------------------------------------------------------
# Malformed sitemap entries must not become requests, or cost page budget
# ---------------------------------------------------------------------------

def test_relative_and_schemeless_sitemap_entries_are_resolved(real_config):
    from efe.fetch.discovery import normalise_sitemap_url

    root = "https://hotel.example"
    assert normalise_sitemap_url("/contact", root) == "https://hotel.example/contact"
    assert normalise_sitemap_url("//cdn.example/x", root) == "https://cdn.example/x"
    assert normalise_sitemap_url("www.hotel.example/kontakt", root) == (
        "https://www.hotel.example/kontakt"
    )
    assert normalise_sitemap_url("https://hotel.example/trade", root) == (
        "https://hotel.example/trade"
    )
    for junk in ("", "   ", "javascript:void(0)", "mailto:a@b.example"):
        assert normalise_sitemap_url(junk, root) == ""


def test_plan_never_emits_a_url_without_a_host(real_config):
    from efe.fetch.discovery import plan_urls

    planned = plan_urls(
        "https://hotel.example/", "hotel.example", "FR", real_config.discovery,
        sitemap_urls=["/contact", "www.hotel.example/kontakt", "", "not a url"],
        page_links=["https://hotel.example/en/trade"],
        limit=8,
    )
    from urllib.parse import urlparse

    for url in planned:
        parsed = urlparse(url)
        assert parsed.scheme in ("http", "https"), url
        assert parsed.netloc, url


async def test_a_hostless_url_is_refused_without_a_request(real_config, tmp_path):
    import httpx

    from efe.fetch.cache import PageCache
    from efe.fetch.client import Fetcher

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Fetcher(real_config.fetch, PageCache(tmp_path, enabled=False),
                       client=client) as fetcher:
        page = await fetcher.get("https:///robots.txt")
        assert not page.ok
        assert "not a usable http(s) URL" in page.error
        assert calls["n"] == 0, "a hostless URL must never reach the network"
