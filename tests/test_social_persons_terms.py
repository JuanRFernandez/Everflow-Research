"""Socials, named contacts and published partner terms."""

from __future__ import annotations

from efe.extract.persons import extract_persons
from efe.extract.social import extract_instagram, extract_linkedin
from efe.extract.terms import extract_terms, is_trade_page, summarise
from tests.conftest import fixture_text


def values(finds):
    return {f.value for f in finds}


# ---------------------------------------------------------------------------
# Socials
# ---------------------------------------------------------------------------


def test_linkedin_company_page_is_extracted(real_config):
    finds = extract_linkedin(fixture_text("socials_footer.html"), real_config.social)
    company = [f for f in finds if not f.extra.get("personal_profile")]
    assert values(company) == {"https://www.linkedin.com/company/summit-lodge-verbier"}


def test_linkedin_personal_profile_is_flagged_not_used(real_config):
    finds = extract_linkedin(fixture_text("socials_footer.html"), real_config.social)
    personal = [f for f in finds if f.extra.get("personal_profile")]
    assert len(personal) == 1
    assert "elena-vargas" in personal[0].value
    assert "not a company page" in personal[0].extra["rejected"]


def test_instagram_handle_excludes_platform_routes(real_config):
    finds = extract_instagram(fixture_text("socials_footer.html"), real_config.social)
    assert "@summitlodgeverbier" in values(finds)
    assert not any(f.value.startswith("@p") and "CabcDEF" in f.value for f in finds)


# ---------------------------------------------------------------------------
# Persons
# ---------------------------------------------------------------------------


def test_name_and_role_pairs_are_extracted():
    finds = extract_persons(fixture_text("team_roles.html"))
    pairs = {f.value: f.extra["role"] for f in finds}
    assert pairs.get("Elena Vargas") == "Director of Sales"
    assert pairs.get("Thomas Brunner") == "General Manager"
    assert pairs.get("Sophie Laurent") == "Reservations Manager"


def test_a_name_with_no_role_is_discarded():
    """Marco Ricci is listed as `Skier`, which is not a role in the vocabulary."""
    finds = extract_persons(fixture_text("team_roles.html"))
    assert "Marco Ricci" not in values(finds)


def test_every_person_carries_a_role():
    for find in extract_persons(fixture_text("team_roles.html")):
        assert find.extra.get("role"), f"{find.value} has no role"


def test_page_furniture_is_not_a_person():
    finds = extract_persons(fixture_text("team_roles.html"))
    assert "Book Now" not in values(finds)
    assert "Our Team" not in values(finds)


def test_empty_body_is_safe():
    assert extract_persons("") == []


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------


def test_trade_page_detection(real_config):
    assert is_trade_page("https://x.example/en/trade", real_config.terms) is True
    assert is_trade_page("https://x.example/travel-agents", real_config.terms) is True
    assert is_trade_page("https://x.example/en/rooms", real_config.terms) is False


def test_terms_are_verbatim_from_a_trade_page(real_config):
    finds = extract_terms(
        fixture_text("trade_terms.html"),
        "https://summitlodge.example/en/trade",
        real_config.terms,
    )
    joined = " ".join(f.value for f in finds)
    assert "standard commission of 10%" in joined
    assert "minimum volume of 20 room nights" in joined
    assert "apply for a trade account" in joined
    # A sentence with no commercial signal is not captured.
    assert "carries no commercial signal" not in joined


def test_stated_percentage_is_surfaced_first(real_config):
    finds = extract_terms(
        fixture_text("trade_terms.html"),
        "https://summitlodge.example/en/trade",
        real_config.terms,
    )
    assert finds[0].extra["has_percentage"] == "true"


def test_terms_are_not_taken_from_a_non_trade_page(real_config):
    assert (
        extract_terms(
            fixture_text("trade_terms.html"),
            "https://summitlodge.example/en/rooms",
            real_config.terms,
        )
        == []
    )


def test_summary_respects_the_cell_length_budget(real_config):
    finds = extract_terms(
        fixture_text("trade_terms.html"),
        "https://summitlodge.example/en/trade",
        real_config.terms,
    )
    assert len(summarise(finds, real_config.terms)) <= real_config.terms.max_chars
