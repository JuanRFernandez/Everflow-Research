"""Category/resort targeting and the FORM-ONLY sentinel."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import pytest

from efe.extract.forms import FORM_ONLY, detect_contact_form
from efe.extract.scope import ScopeDecision, ScopeGuard
from efe.fetch.cache import CachedPage, PageCache
from efe.fetch.client import Fetcher
from efe.models import Confidence, Field_, PageKind, ScopeVerdict
from efe.pipeline import Enricher, FetchedPage
from efe.workbook.reader import resort_matches, select_candidates

# ---------------------------------------------------------------------------
# Resort matching: accents, umlauts and both German spellings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cell", "wanted", "expected"),
    [
        ("Kitzbuehel", ["Kitzbühel"], True),
        ("Kitzbühel", ["Kitzbuehel"], True),
        ("Zurs am Arlberg", ["Zürs"], True),
        ("Zürs", ["Zurs"], True),
        ("St. Anton am Arlberg", ["St. Anton"], True),
        ("St Christoph am Arlberg", ["St. Christoph"], True),
        ("Garmisch-Partenkirchen", ["Garmisch"], True),
        ("Neustift im Stubaital", ["Stubai"], True),
        ("Zell am See-Kaprun", ["Zell am See"], True),
        ("Courchevel 1850", ["Kitzbühel", "Lech"], False),
        ("Val d'Isere", ["Seefeld"], False),
        ("anything", [], True),          # empty filter = all rows
    ],
)
def test_resort_matches(cell, wanted, expected):
    assert resort_matches(cell, wanted) is expected


def _view_of(real_config, rows_spec):
    """A throwaway WorkbookView from (row, name, category, resort, domain) tuples."""
    from pathlib import Path

    from efe.workbook.reader import PartnerRow, WorkbookView

    spec = real_config.workbook
    rows = []
    for row, name, category, resort, domain in rows_spec:
        cells = {
            spec.column_for("id"): f"EFE-{row:04d}",
            spec.column_for("entity_name"): name,
            spec.column_for("category"): category,
            spec.column_for("resort_base"): resort,
            spec.column_for("website_url"): f"https://{domain}",
            spec.column_for("contacted"): "NO",
        }
        rows.append(PartnerRow(row=row, cells=cells))
    return WorkbookView(path=Path("x.xlsx"), rows=rows, formula_count=0)


def test_selection_filters_by_category_and_resort(real_config):
    view = _view_of(real_config, [
        (2, "Hotel Lech", "1. Hotels", "Lech", "hotel-lech.example"),
        (3, "Chalet Seefeld", "2. Chalets & Chalet Management", "Seefeld", "cs.example"),
        (4, "Agencia SP", "6. Distribution & Sales Agencies", "n/a", "agencia.example"),
        (5, "Hotel Verbier", "1. Hotels", "Verbier", "hv.example"),
    ])

    real_config.selection.categories = ["1.", "2.", "3."]
    real_config.selection.resorts = []
    candidates, skipped = select_candidates(view, real_config)
    assert {c.name for c in candidates} == {"Hotel Lech", "Chalet Seefeld", "Hotel Verbier"}
    assert any("outside the target categories" in r for r in skipped.values())

    real_config.selection.resorts = ["Lech", "Seefeld"]
    candidates, skipped = select_candidates(view, real_config)
    assert {c.name for c in candidates} == {"Hotel Lech", "Chalet Seefeld"}
    assert any("outside the target resorts" in r for r in skipped.values())

    # clearing both restores the old behaviour
    real_config.selection.categories = []
    real_config.selection.resorts = []
    unfiltered, _ = select_candidates(view, real_config)
    assert {c.name for c in unfiltered} == {"Hotel Lech", "Chalet Seefeld",
                                            "Agencia SP", "Hotel Verbier"}


# ---------------------------------------------------------------------------
# Contact-form detection
# ---------------------------------------------------------------------------

CONTACT_FORM = (
    '<html><body><h1>Kontakt</h1>'
    '<form id="contact" action="/kontakt/senden">'
    '<input type="text" name="name"><input type="email" name="email">'
    '<textarea name="nachricht"></textarea></form></body></html>'
)


def test_a_contact_form_is_detected():
    find = detect_contact_form(CONTACT_FORM)
    assert find is not None
    assert find.value == FORM_ONLY
    assert find.matched_text.startswith("<form")


@pytest.mark.parametrize(
    "body",
    [
        '<html><body><form action="/search"><input name="q"></form></body></html>',
        '<html><body><form id="login"><input type="email" name="email">'
        '<input type="password" name="pw"></form></body></html>',
        '<html><body><form class="newsletter-signup"><input type="email" '
        'name="email"></form></body></html>',
        "<html><body><p>no form here</p></body></html>",
        "",
    ],
)
def test_search_login_and_newsletter_forms_do_not_count(body):
    assert detect_contact_form(body) is None


# ---------------------------------------------------------------------------
# FORM-ONLY through the pipeline
# ---------------------------------------------------------------------------

def _enricher(cfg, tmp_path):
    return Enricher(
        cfg, Fetcher(cfg.fetch, PageCache(tmp_path, enabled=False)), ScopeGuard(cfg.scope)
    )


def _page(body, url="https://hotel.example/kontakt", kind=PageKind.CONTACT,
          verdict=ScopeVerdict.OWN_DOMAIN):
    return FetchedPage(
        url=url, kind=kind,
        page=CachedPage(url=url, final_url=url, status=200, body=body,
                        fetched_at=datetime(2026, 8, 25),
                        headers={"content-type": "text/html"}),
        scope=ScopeDecision(verdict=verdict, reason="test"),
    )


def _candidate(real_config, **existing):
    from efe.models import Candidate

    base = dict.fromkeys(
        ("general_email", "sales_b2b_email", "phone", "whatsapp",
         "contact_person_name", "contact_person_role", "linkedin_url",
         "instagram_handle", "commission_terms"), "TBD")
    base.update(existing)
    return Candidate(entity_id="EFE-9999", row=2, name="Hotel Test",
                     website_url="https://hotel.example", domain="hotel.example",
                     country="AT", existing=base)


def test_form_only_written_when_no_email_anywhere(real_config, tmp_path):
    enricher = _enricher(real_config, tmp_path)
    values = enricher.extract(_candidate(real_config), [_page(CONTACT_FORM)])
    form_values = [v for v in values if v.value == FORM_ONLY]
    assert len(form_values) == 1
    v = form_values[0]
    assert v.field is Field_.GENERAL_EMAIL
    assert v.confidence is Confidence.HIGH and v.writable
    assert v.evidence.source_url == "https://hotel.example/kontakt"


def test_a_real_email_suppresses_form_only(real_config, tmp_path):
    body = CONTACT_FORM.replace(
        "<h1>Kontakt</h1>",
        '<h1>Kontakt</h1><a href="mailto:info@hotel.example">mail</a>')
    enricher = _enricher(real_config, tmp_path)
    values = enricher.extract(_candidate(real_config), [_page(body)])
    assert any(v.value == "info@hotel.example" for v in values)
    assert not any(v.value == FORM_ONLY for v in values)


def test_form_only_not_repeated_when_already_recorded(real_config, tmp_path):
    enricher = _enricher(real_config, tmp_path)
    candidate = _candidate(real_config, general_email="FORM-ONLY")
    values = enricher.extract(candidate, [_page(CONTACT_FORM)])
    assert not any(v.value == FORM_ONLY for v in values)


def test_form_only_blocked_on_unmatched_group_page(real_config, tmp_path):
    enricher = _enricher(real_config, tmp_path)
    page = _page(CONTACT_FORM, verdict=ScopeVerdict.SHARED_UNMATCHED)
    values = enricher.extract(_candidate(real_config), [page])
    assert not any(v.value == FORM_ONLY for v in values)


def test_form_only_is_replaceable(real_config):
    """FORM-ONLY sits in empty_tokens: a later real address may overwrite it."""
    assert real_config.workbook.is_empty("FORM-ONLY") is True
    assert real_config.workbook.is_empty("info@hotel.example") is False


def test_config_targets_active_for_hotels(real_config):
    assert real_config.selection.categories == ["1.", "2.", "3."]
    resorts = Counter(real_config.selection.resorts)
    assert resorts["Kitzbühel"] == 1 and resorts["Zell am See"] == 1
    assert real_config.confidence.form_only_when_no_email is True
