"""Impressum parsing, the group-domain scope guard, and page discovery."""

from __future__ import annotations

from collections import Counter

import pytest

from efe.extract.impressum import extract_impressum, looks_like_impressum
from efe.extract.scope import ScopeGuard, tokenise
from efe.fetch.discovery import (
    candidate_paths,
    classify_page,
    contact_links,
    filter_sitemap_urls,
    parse_sitemap,
    plan_urls,
    wants_impressum_first,
)
from efe.fetch.robots import RobotsCache
from efe.models import PageKind, ScopeVerdict
from tests.conftest import fixture_text

# ---------------------------------------------------------------------------
# Impressum -- the highest-yield page in the dataset, handled first-class
# ---------------------------------------------------------------------------

def test_impressum_is_recognised():
    body = fixture_text("impressum_de.html")
    assert looks_like_impressum("https://x.example/impressum", body) is True
    assert looks_like_impressum("https://x.example/anything", body) is True
    assert looks_like_impressum("https://x.example/rooms", "<html>rooms</html>") is False


def test_labelled_fields_are_parsed():
    parsed = extract_impressum(
        fixture_text("impressum_de.html"), "https://x.example/impressum"
    )
    assert "+49 8821 123456" in {f.value for f in parsed["phone"]}
    assert "info@silberdistel-berghotel.example" in {f.value for f in parsed["email"]}
    assert "Katharina Meier" in {f.value for f in parsed["person"]}


def test_representative_carries_its_legal_role():
    parsed = extract_impressum(
        fixture_text("impressum_de.html"), "https://x.example/impressum"
    )
    roles = {f.extra["role"] for f in parsed["person"]}
    assert roles & {"Geschäftsführer", "Vertretungsberechtigter (Impressum)"}


def test_registry_identifiers_are_captured():
    parsed = extract_impressum(
        fixture_text("impressum_de.html"), "https://x.example/impressum"
    )
    values = {f.value for f in parsed["registry"]}
    assert any("12345" in v for v in values)
    assert any("DE123456789" in v.replace(" ", "") for v in values)


def test_impressum_finds_carry_evidence():
    parsed = extract_impressum(
        fixture_text("impressum_de.html"), "https://x.example/impressum"
    )
    for kind in ("phone", "email", "person"):
        for find in parsed[kind]:
            assert find.matched_text.strip()
            assert find.method.startswith("impressum-label")


def test_empty_body_is_safe():
    parsed = extract_impressum("", "https://x.example/impressum")
    assert parsed == {"phone": [], "email": [], "person": [], "registry": []}


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------

@pytest.fixture
def guard(real_config):
    # hotelsbarriere.example stands in for a domain two PARTNERS rows share.
    counts = Counter({"hotelsbarriere.example": 2, "own-hotel.example": 1})
    return ScopeGuard(real_config.scope, counts)


def test_own_domain_needs_no_matching(guard):
    decision = guard.decide(
        entity_name="The Alpina Gstaad",
        domain="own-hotel.example",
        resort_base="Gstaad",
        page_url="https://own-hotel.example/contact",
    )
    assert decision.verdict is ScopeVerdict.OWN_DOMAIN
    assert decision.accepted is True


def test_domain_shared_by_two_rows_is_shared(guard):
    assert guard.is_shared("hotelsbarriere.example") is True
    assert guard.is_shared("own-hotel.example") is False


def test_configured_chain_domain_is_shared_even_with_one_row(real_config):
    guard = ScopeGuard(real_config.scope, Counter({"marriott.com": 1}))
    assert guard.is_shared("marriott.com") is True


def test_group_contact_page_is_rejected_for_a_property(real_config):
    """The W Verbier case: a group contact page must not fill the property row."""
    guard = ScopeGuard(real_config.scope, Counter({"grandclass.example": 2}))
    decision = guard.decide(
        entity_name="W Verbier",
        domain="grandclass.example",
        resort_base="Verbier",
        page_url="https://grandclass.example/contact",
        identity_text="Contact Us | Grandclass Hotels",
    )
    assert decision.verdict is ScopeVerdict.SHARED_UNMATCHED
    assert decision.accepted is False
    assert "property-level Website_URL" in decision.reason


def test_property_page_on_a_group_domain_is_accepted(real_config):
    guard = ScopeGuard(real_config.scope, Counter({"grandclass.example": 2}))
    decision = guard.decide(
        entity_name="W Verbier",
        domain="grandclass.example",
        resort_base="Verbier",
        page_url="https://grandclass.example/hotels/w-verbier/contact",
        identity_text="W Verbier - Contact | Grandclass Hotels",
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED
    assert "verbier" in decision.matched_tokens


def test_mega_menu_alone_does_not_match(real_config):
    """A chain nav lists every property. Body text must not be enough to match."""
    guard = ScopeGuard(real_config.scope, Counter({"grandclass.example": 2}))
    decision = guard.decide(
        entity_name="W Verbier",
        domain="grandclass.example",
        resort_base="Verbier",
        page_url="https://grandclass.example/contact",
        identity_text="Contact Us | Grandclass Hotels",  # nav text excluded by design
    )
    assert decision.verdict is ScopeVerdict.SHARED_UNMATCHED


def test_domain_tokens_do_not_count_as_distinguishing(real_config):
    """`airelles` on airelles.com proves nothing about which Airelles property."""
    guard = ScopeGuard(real_config.scope, Counter({"airelles.com": 2}))
    tokens = guard.discriminating_tokens("Airelles Val d'Isere (Mademoiselle)", "airelles.com")
    assert "airelles" not in tokens
    assert "isere" in tokens


def test_row_that_is_the_group_itself_is_accepted(real_config):
    guard = ScopeGuard(real_config.scope, Counter({"oetkercollection.com": 2}))
    decision = guard.decide(
        entity_name="Oetker Collection",
        domain="oetkercollection.com",
        resort_base="",
        page_url="https://oetkercollection.com/contact",
        identity_text="Contact",
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED
    assert "the group itself" in decision.reason


def test_resort_base_can_carry_the_match(real_config):
    guard = ScopeGuard(real_config.scope, Counter({"grandclass.example": 2}))
    decision = guard.decide(
        entity_name="Grandclass Mountain Retreat",
        domain="grandclass.example",
        resort_base="Megeve",
        page_url="https://grandclass.example/destinations/megeve",
        identity_text="Megeve",
    )
    assert decision.verdict is ScopeVerdict.SHARED_MATCHED
    assert "resort base" in decision.reason


def test_accents_fold_for_matching(real_config):
    stopwords = {"hotel"}
    assert tokenise("Val d'Isère", stopwords) == ["val", "isere"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://x.example/", PageKind.HOME),
        ("https://x.example/impressum", PageKind.IMPRESSUM),
        ("https://x.example/en/contact", PageKind.CONTACT),
        ("https://x.example/kontakt", PageKind.CONTACT),
        ("https://x.example/contatti", PageKind.CONTACT),
        ("https://x.example/en/trade", PageKind.TRADE),
        ("https://x.example/travel-agents", PageKind.TRADE),
        ("https://x.example/mentions-legales", PageKind.LEGAL),
        ("https://x.example/our-team", PageKind.TEAM),
        ("https://x.example/about-us", PageKind.ABOUT),
        ("https://x.example/rooms", PageKind.OTHER),
    ],
)
def test_page_classification(url, kind):
    assert classify_page(url) is kind


def test_sitemap_parsing():
    urls, children = parse_sitemap(fixture_text("sitemap.xml"))
    assert "https://summitlodge.example/en/contact" in urls
    assert children == []

    urls, children = parse_sitemap(fixture_text("sitemap_index.xml"))
    assert urls == []
    assert "https://summitlodge.example/sitemap-pages.xml" in children

    assert parse_sitemap("not xml at all") == ([], [])


def test_sitemap_filtering_prefers_contact_routes(real_config):
    urls, _ = parse_sitemap(fixture_text("sitemap.xml"))
    kept = filter_sitemap_urls(urls, real_config.discovery, limit=10)
    assert kept[0].endswith("/impressum")
    assert "https://summitlodge.example/en/contact" in kept
    assert "https://summitlodge.example/en/rooms" not in kept


def test_in_page_contact_links(real_config):
    links = contact_links(
        fixture_text("homepage_with_links.html"),
        "https://summitlodge.example/",
        real_config.discovery,
    )
    assert "https://summitlodge.example/en/contact" in links
    assert "https://summitlodge.example/en/trade" in links
    # Off-site, mailto and anchor links are not followed.
    assert not any("booking.elsewhere.example" in u for u in links)
    assert not any(u.startswith("mailto:") for u in links)


def test_impressum_goes_first_for_de_and_at(real_config):
    assert wants_impressum_first("hotel.de", "DE", real_config.discovery) is True
    assert wants_impressum_first("hotel.example", "AT", real_config.discovery) is True
    assert wants_impressum_first("hotel.at", "", real_config.discovery) is True
    assert wants_impressum_first("hotel.fr", "FR", real_config.discovery) is False


def test_plan_puts_impressum_first_for_a_german_site(real_config):
    planned = plan_urls(
        "https://silberdistel.de/", "silberdistel.de", "DE", real_config.discovery, limit=6
    )
    assert planned[0] == "https://silberdistel.de/impressum"


def test_plan_uses_sitemap_before_blind_probing(real_config):
    urls, _ = parse_sitemap(fixture_text("sitemap.xml"))
    planned = plan_urls(
        "https://summitlodge.example/", "summitlodge.example", "CH",
        real_config.discovery, sitemap_urls=urls, limit=4,
    )
    assert "https://summitlodge.example/impressum" in planned
    assert "https://summitlodge.example/en/contact" in planned


def test_plan_never_repeats_a_url(real_config):
    urls, _ = parse_sitemap(fixture_text("sitemap.xml"))
    planned = plan_urls(
        "https://summitlodge.example/", "summitlodge.example", "CH",
        real_config.discovery, sitemap_urls=urls,
        page_links=["https://summitlodge.example/en/contact"], limit=8,
    )
    assert len(planned) == len(set(planned))
    assert len(planned) <= 8


def test_candidate_paths_expand_across_languages(real_config):
    paths = candidate_paths(real_config.discovery)
    assert "/contact" in paths
    assert "/de/impressum" in paths
    assert len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def test_robots_disallow_is_obeyed():
    cache = RobotsCache("EverFlowResearchBot/0.1", respect=True)
    policy = cache.record("summitlodge.example", fixture_text("robots_disallow.txt"))
    assert policy.crawl_delay == 5
    assert "https://summitlodge.example/sitemap.xml" in policy.sitemaps
    assert cache.allows("summitlodge.example", "https://summitlodge.example/en/contact") is True
    assert cache.allows("summitlodge.example", "https://summitlodge.example/private/x") is False
    assert "https://summitlodge.example/private/x" in cache.blocked_urls


def test_missing_robots_means_allowed():
    cache = RobotsCache("EverFlowResearchBot/0.1", respect=True)
    cache.record("nope.example", None, error="404")
    assert cache.allows("nope.example", "https://nope.example/contact") is True
