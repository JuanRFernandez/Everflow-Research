"""Review-queue deduplication, the per-field cap, and the failing-domain breaker.

The first real dry run held 1330 candidates for 20 rows, 958 of them the same value
re-found on another page: group sites repeat every property's phone number in the
footer of every page. These are the guards that keep the review queue readable.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from efe.fetch.cache import CachedPage, PageCache
from efe.fetch.client import Fetcher
from efe.models import (
    Candidate,
    Confidence,
    DataClass,
    Evidence,
    ExtractedValue,
    Field_,
    PageKind,
)
from efe.pipeline import choose_values

NOW = datetime(2026, 8, 21, 12, 0, 0)


def value(address, url, kind=PageKind.CONTACT, field=Field_.GENERAL_EMAIL,
          confidence=Confidence.HIGH):
    return ExtractedValue(
        field=field,
        value=address,
        confidence=confidence,
        data_class=DataClass.CORPORATE_ROLE,
        evidence=Evidence(
            source_url=url, matched_text=address, byte_offset=0,
            fetched_at=NOW, page_kind=kind,
        ),
        extractor="tests",
    )


def candidate():
    return Candidate(
        entity_id="EFE-0001", row=2, name="Test", website_url="https://x.example",
        domain="x.example",
        existing=dict.fromkeys(
            ("general_email", "sales_b2b_email", "phone", "whatsapp",
             "contact_person_name", "contact_person_role", "linkedin_url",
             "instagram_handle", "commission_terms"),
            "TBD",
        ),
    )


# ---------------------------------------------------------------------------
# Dedupe and cap
# ---------------------------------------------------------------------------

def test_the_same_value_on_many_pages_is_recorded_once(real_config):
    """A footer address appears on every page. It is one candidate, not eight."""
    pages = [f"https://x.example/page-{n}" for n in range(8)]
    values = [value("info@x.example", url) for url in pages]
    values += [value("reservations@x.example", url) for url in pages]

    chosen, held, dropped = choose_values(candidate(), values, real_config)

    assert chosen[Field_.GENERAL_EMAIL].value == "info@x.example"
    assert [h.value for h in held] == ["reservations@x.example"]
    assert dropped == 0


def test_the_best_evidenced_occurrence_survives_deduplication(real_config):
    """Of eight copies, the one from the strongest page kind is the one kept."""
    values = [
        value("info@x.example", "https://x.example/", kind=PageKind.HOME),
        value("info@x.example", "https://x.example/impressum", kind=PageKind.IMPRESSUM),
        value("info@x.example", "https://x.example/about", kind=PageKind.ABOUT),
    ]
    chosen, _, _ = choose_values(candidate(), values, real_config)
    assert chosen[Field_.GENERAL_EMAIL].evidence.page_kind is PageKind.IMPRESSUM


def test_alternates_are_capped_and_the_overflow_is_counted(real_config):
    cap = real_config.review.max_alternates_per_field
    values = [
        value(f"info{n}@x.example", f"https://x.example/p{n}") for n in range(cap + 6)
    ]
    chosen, held, dropped = choose_values(candidate(), values, real_config)

    assert len(chosen) == 1
    assert len(held) == cap
    # cap + 6 candidates, one written, `cap` held, the rest counted not discarded silently.
    assert dropped == (cap + 6) - 1 - cap


def test_nothing_is_dropped_when_under_the_cap(real_config):
    values = [value(f"info{n}@x.example", f"https://x.example/p{n}") for n in range(3)]
    _, held, dropped = choose_values(candidate(), values, real_config)
    assert len(held) == 2
    assert dropped == 0


def test_cap_is_per_field_not_per_row(real_config):
    cap = real_config.review.max_alternates_per_field
    values = [
        value(f"info{n}@x.example", f"https://x.example/p{n}") for n in range(cap + 3)
    ]
    values += [
        value(f"+4179555{n:04d}", f"https://x.example/p{n}", field=Field_.PHONE)
        for n in range(cap + 3)
    ]
    _, held, _ = choose_values(candidate(), values, real_config)
    per_field = {}
    for item in held:
        per_field[item.field] = per_field.get(item.field, 0) + 1
    assert per_field[Field_.GENERAL_EMAIL] == cap
    assert per_field[Field_.PHONE] == cap


# ---------------------------------------------------------------------------
# Failing-domain circuit breaker
# ---------------------------------------------------------------------------

@pytest.fixture
def counting_fetcher(real_config, tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    real_config.fetch.per_domain_delay_seconds = 0.0
    real_config.fetch.max_retries = 1
    cache = PageCache(tmp_path / "cache", enabled=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(real_config.fetch, cache, client=client), calls


@pytest.mark.asyncio
async def test_a_domain_answering_429_is_abandoned(counting_fetcher, real_config):
    fetcher, calls = counting_fetcher
    async with fetcher:
        for n in range(12):
            await fetcher.get(f"https://busy.example/page-{n}")

    assert "busy.example" in fetcher.skipped_domains
    assert "consecutive failures" in fetcher.skipped_domains["busy.example"]
    # robots.txt plus a handful of pages, not all twelve.
    assert calls["n"] <= real_config.fetch.max_consecutive_failures + 2


@pytest.mark.asyncio
async def test_a_404_does_not_count_toward_abandoning(real_config, tmp_path):
    """Probing candidate paths produces 404s by design; they are normal misses."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404, text="no such page")

    real_config.fetch.per_domain_delay_seconds = 0.0
    cache = PageCache(tmp_path / "cache", enabled=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Fetcher(real_config.fetch, cache, client=client) as fetcher:
        for n in range(10):
            await fetcher.get(f"https://quiet.example/probe-{n}")
    assert fetcher.skipped_domains == {}


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_run(real_config, tmp_path):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] % 3 == 0:
            return httpx.Response(200, text="<html>ok</html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(503, text="down")

    real_config.fetch.per_domain_delay_seconds = 0.0
    real_config.fetch.max_retries = 1
    cache = PageCache(tmp_path / "cache", enabled=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Fetcher(real_config.fetch, cache, client=client) as fetcher:
        for n in range(9):
            await fetcher.get(f"https://flaky.example/page-{n}")
    assert fetcher.skipped_domains == {}


def test_note_outcome_is_pure_bookkeeping(real_config, tmp_path):
    """The breaker only reads status and error; it never re-requests anything."""
    fetcher = Fetcher(real_config.fetch, PageCache(tmp_path / "c", enabled=False))
    failure = CachedPage(url="https://x.example/", final_url="https://x.example/",
                         status=0, fetched_at=NOW, error="HTTP 429")
    for _ in range(real_config.fetch.max_consecutive_failures):
        fetcher._note_outcome("x.example", failure)
    assert "x.example" in fetcher.skipped_domains
