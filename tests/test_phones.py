"""Phone normalisation to E.164, and the stricter WhatsApp rule."""

from __future__ import annotations

import pytest

from efe.extract.phones import extract_phones, extract_whatsapp, region_for, to_e164
from tests.conftest import fixture_text


def values(finds):
    return {f.value for f in finds}


@pytest.mark.parametrize(
    ("fixture", "region", "expected"),
    [
        ("kontakt_at.html", "AT", "+43535612340"),
        ("contact_fr.html", "FR", "+33479001122"),
        ("contatti_it.html", "IT", "+390474100200"),
        ("contacto_es.html", "MX", "+525512345678"),
        ("whatsapp_links.html", "CH", "+41279661234"),
    ],
)
def test_tel_hrefs_normalise(real_config, fixture, region, expected):
    finds = extract_phones(fixture_text(fixture), region, real_config.phone)
    assert expected in values(finds)


def test_tel_href_wins_provenance(real_config):
    finds = extract_phones(fixture_text("contact_fr.html"), "FR", real_config.phone)
    by_value = {f.value: f for f in finds}
    assert by_value["+33479001122"].method == "tel-href"


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        ("+41 27 966 03 03", None, "+41279660303"),
        ("027 966 03 03", "CH", "+41279660303"),
        ("08821 123456", "DE", "+498821123456"),
        # libphonenumber correctly drops the national trunk prefix "(0)".
        ("+49 (0) 8821 123456", "DE", "+498821123456"),
        ("(11) 3456-7890", "BR", "+551134567890"),
        ("12345", "CH", None),
        ("not a number", "CH", None),
        ("99999999999999999999", "CH", None),
    ],
)
def test_to_e164(real_config, raw, region, expected):
    assert to_e164(raw, region, real_config.phone) == expected


def test_region_hint_from_country_then_tld(real_config):
    assert region_for("CH", "example.com", real_config.phone) == "CH"
    assert region_for("Brasil", "example.com", real_config.phone) == "BR"
    assert region_for("", "hotel.co.uk", real_config.phone) == "GB"
    assert region_for("", "hotel.com.br", real_config.phone) == "BR"
    assert region_for("", "hotel.example", real_config.phone) is None


def test_whatsapp_only_from_explicit_sources(real_config):
    body = fixture_text("whatsapp_links.html")
    found = values(extract_whatsapp(body, "CH", real_config.phone))
    assert "+41795552030" in found  # api.whatsapp.com link and a labelled number
    assert "+41796667788" in found  # wa.me link
    # The office landline is a tel: link with no WhatsApp label anywhere near it.
    assert "+41279661234" not in found


def test_whatsapp_is_never_derived_from_phone(real_config):
    """A page with a phone number and no WhatsApp marker yields no WhatsApp value."""
    body = fixture_text("contact_fr.html")
    assert extract_whatsapp(body, "FR", real_config.phone) == []
    assert extract_phones(body, "FR", real_config.phone) != []


def test_empty_body_is_safe(real_config):
    assert extract_phones("", "CH", real_config.phone) == []
    assert extract_whatsapp("", "CH", real_config.phone) == []
