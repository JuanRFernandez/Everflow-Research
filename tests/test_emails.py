"""Email extraction, including every obfuscation form the crawler handles."""

from __future__ import annotations

import pytest

from efe.extract.emails import acceptable, extract_emails
from tests.conftest import fixture_text


def values(finds):
    return {f.value for f in finds}


def test_mailto_and_plain_text(real_config):
    finds = extract_emails(fixture_text("contact_fr.html"), real_config.email)
    found = values(finds)
    assert "contact@chalet-belle-etoile.example" in found
    assert "agences@chalet-belle-etoile.example" in found
    # plain text, no mailto
    assert "reservations@chalet-belle-etoile.example" in found
    # noreply is configured noise and never survives
    assert "noreply@chalet-belle-etoile.example" not in found


def test_mailto_wins_provenance(real_config):
    finds = extract_emails(fixture_text("contact_fr.html"), real_config.email)
    by_value = {f.value: f for f in finds}
    assert by_value["contact@chalet-belle-etoile.example"].method == "mailto"
    assert by_value["reservations@chalet-belle-etoile.example"].method == "plain-text"


@pytest.mark.parametrize(
    ("address", "method"),
    [
        ("info@obfuscated-hotel.example", "obfuscated"),  # info [at] x [dot] example
        ("sales@obfuscated-hotel.example", "obfuscated"),  # sales (at) x.example
        ("reception@obfuscated-hotel.example", "plain-text"),  # &#114;eception&#64;
        ("office@obfuscated-hotel.example", "mailto"),  # %6Fffice@ percent-encoded
        ("affairs@obfuscated-hotel.example", "css-reversed"),  # rtl bidi-override span
        ("kontakt@obfuscated-hotel.example", "data-attribute"),
        ("booking@obfuscated-hotel.example", "js-concat"),
    ],
)
def test_obfuscated_forms(real_config, address, method):
    finds = extract_emails(fixture_text("obfuscated_emails.html"), real_config.email)
    by_value = {f.value: f for f in finds}
    assert address in by_value, f"{address} was not extracted"
    assert by_value[address].method == method


def test_vendor_and_noise_rejected(real_config):
    finds = extract_emails(fixture_text("obfuscated_emails.html"), real_config.email)
    found = values(finds)
    assert not any("wixpress" in a for a in found)
    assert "noreply@obfuscated-hotel.example" not in found


def test_image_only_address_is_not_invented(real_config):
    """The fixture has an <img alt='email'>. Nothing may be produced from it."""
    finds = extract_emails(fixture_text("obfuscated_emails.html"), real_config.email)
    assert not any(f.value.endswith(".png") for f in finds)
    assert all("@" in f.value for f in finds)


def test_every_find_carries_its_literal_match(real_config):
    for name in ("contact_fr.html", "contatti_it.html", "obfuscated_emails.html"):
        for find in extract_emails(fixture_text(name), real_config.email):
            assert find.matched_text.strip(), f"{find.value} has no matched_text"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("info@hotel.example", True),
        ("noreply@hotel.example", False),
        ("webmaster@hotel.example", False),
        ("info@example.com", False),
        ("sales@wixpress.com", False),
        ("a@b.co", True),
    ],
)
def test_acceptable(real_config, address, expected):
    assert acceptable(address, real_config.email)[0] is expected


def test_empty_body_is_safe(real_config):
    assert extract_emails("", real_config.email) == []
