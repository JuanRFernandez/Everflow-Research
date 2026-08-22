"""The General_Email vs Sales_B2B_Email decision.

Every case asserts the routing *and* that the stated reason names the token that
drove it -- the reason string is what appears in `--dry-run` output, so it has to be
true, not decorative.
"""

from __future__ import annotations

import pytest

from efe.extract.classify import (
    classify_email,
    domain_matches_site,
    is_freemail,
    is_personal_local_part,
    local_part_of,
)
from efe.extract.emails import extract_emails
from efe.models import DataClass, Field_, PageKind
from tests.conftest import fixture_text


def route(real_config, address, kind=PageKind.CONTACT):
    return classify_email(address, kind, real_config.email, real_config.gdpr)


@pytest.mark.parametrize(
    ("address", "token"),
    [
        ("sales@x.example", "sales"),
        ("trade@x.example", "trade"),
        ("b2b@x.example", "b2b"),
        ("partners@x.example", "partners"),
        ("groups@x.example", "groups"),
        ("traveltrade@x.example", "traveltrade"),
        ("travel-trade@x.example", "trade"),
        ("agences@x.example", "agences"),
        ("agenzie@x.example", "agenzie"),
        ("adv@x.example", "adv"),
        ("ventas@x.example", "ventas"),
        ("vertrieb@x.example", "vertrieb"),
        ("mice@x.example", "mice"),
        ("sales.megeve@x.example", "sales"),
    ],
)
def test_sales_addresses_route_to_column_j(real_config, address, token):
    routing = route(real_config, address)
    assert routing.field is Field_.SALES_B2B_EMAIL
    assert routing.writable is True
    assert routing.data_class is DataClass.CORPORATE_ROLE
    assert token in routing.reason


@pytest.mark.parametrize(
    ("address", "token"),
    [
        ("info@x.example", "info"),
        ("contact@x.example", "contact"),
        ("kontakt@x.example", "kontakt"),
        ("office@x.example", "office"),
        ("reception@x.example", "reception"),
        ("hello@x.example", "hello"),
        ("reservations@x.example", "reservations"),
        ("booking@x.example", "booking"),
        ("enquiries@x.example", "enquiries"),
    ],
)
def test_general_addresses_route_to_column_i(real_config, address, token):
    routing = route(real_config, address)
    assert routing.field is Field_.GENERAL_EMAIL
    assert routing.writable is True
    assert token in routing.reason


def test_sales_token_beats_general_token(real_config):
    """`info.sales@` carries both; the trade token decides."""
    routing = route(real_config, "info.sales@x.example")
    assert routing.field is Field_.SALES_B2B_EMAIL
    assert "trade/B2B token" in routing.reason


def test_general_address_on_trade_page_stays_general_but_is_noted(real_config):
    routing = route(real_config, "info@x.example", PageKind.TRADE)
    assert routing.field is Field_.GENERAL_EMAIL
    assert routing.writable is True
    assert "trade/B2B page" in routing.reason


def test_named_individual_is_never_written(real_config):
    for address in ("k.meier@x.example", "thomas.brunner@x.example", "anna_bauer@x.example"):
        routing = route(real_config, address)
        assert routing.field is None
        assert routing.writable is False
        assert routing.data_class is DataClass.PERSONAL_NAMED
        assert "GDPR" in routing.reason


def test_role_token_beats_a_personal_looking_shape(real_config):
    """`travel.trade@` matches the firstname.lastname pattern but is a role address."""
    assert is_personal_local_part("travel.trade", real_config.gdpr) is True
    routing = route(real_config, "travel.trade@x.example")
    assert routing.field is Field_.SALES_B2B_EMAIL
    assert routing.data_class is DataClass.CORPORATE_ROLE


def test_unrecognised_local_part_is_held_not_written(real_config):
    routing = route(real_config, "zimmerwelt@x.example")
    assert routing.writable is False
    assert routing.data_class is DataClass.UNKNOWN
    assert "never written" in routing.reason
    assert "config.yaml" in routing.reason


def test_unrecognised_local_part_prefers_sales_on_a_trade_page(real_config):
    routing = route(real_config, "zimmerwelt@x.example", PageKind.TRADE)
    assert routing.field is Field_.SALES_B2B_EMAIL
    assert routing.writable is False


def test_plus_tag_is_stripped(real_config):
    assert local_part_of("info+web@x.example") == "info"
    assert route(real_config, "info+web@x.example").field is Field_.GENERAL_EMAIL


def test_segment_matching_not_substring_matching(real_config):
    """`partnersupport` must not match the `partner` token."""
    routing = route(real_config, "partnersupport@x.example")
    assert routing.writable is False


def test_end_to_end_routing_on_the_fixtures(real_config):
    """Every address in the personal-emails fixture lands where it should."""
    finds = extract_emails(fixture_text("personal_emails.html"), real_config.email)
    decided = {
        f.value: classify_email(f.value, PageKind.CONTACT, real_config.email, real_config.gdpr)
        for f in finds
    }
    assert decided["info@personal-test.example"].field is Field_.GENERAL_EMAIL
    assert decided["info@personal-test.example"].writable is True
    assert decided["travel.trade@personal-test.example"].field is Field_.SALES_B2B_EMAIL
    assert decided["travel.trade@personal-test.example"].writable is True
    for personal in ("k.meier@personal-test.example", "thomas.brunner@personal-test.example"):
        assert decided[personal].writable is False
        assert decided[personal].data_class is DataClass.PERSONAL_NAMED
    assert decided["zimmerwelt@personal-test.example"].writable is False


def test_freemail_and_domain_matching(real_config):
    assert is_freemail("someone@gmail.com", real_config.email) is True
    assert is_freemail("info@hotel.example", real_config.email) is False
    assert domain_matches_site("info@hotel.example", "hotel.example") is True
    assert domain_matches_site("info@mail.hotel.example", "hotel.example") is True
    assert domain_matches_site("info@other.example", "hotel.example") is False
