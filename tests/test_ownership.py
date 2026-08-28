"""Column ownership: who may write what, in every row.

The rule this file pins replaces a row-level one. Until 2026-08-29 a row with
`Contacted = YES` was frozen whole, so the moment Juan mailed a hotel, the tool
stopped filling in that hotel's missing WhatsApp and LinkedIn -- exactly when the
row started to matter. Ownership is per column now: the CRM block and the
negotiated commission are his in every row, and everything else stays fillable in
every row.
"""

from __future__ import annotations

import pytest
import yaml

from efe import config as config_mod
from efe.cli import parse_cols, parse_rows
from efe.models import Field_
from efe.pipeline import choose_values
from efe.report import render_paste_block
from tests.test_noise_control import candidate, value


def _held(held, field):
    return [(v, why) for v, why in held if v.field is field]


# ---------------------------------------------------------------------------
# The config contract: every column has exactly one owner
# ---------------------------------------------------------------------------


def test_every_column_of_the_real_config_has_exactly_one_owner(real_config):
    wb = real_config.workbook
    lists = {
        "identity": wb.identity_columns,
        "research": wb.research_columns,
        "contact": wb.contact_columns,
        "provenance": wb.provenance_columns,
        "protected": wb.protected_columns,
    }
    owned = [name for names in lists.values() for name in names]
    assert sorted(owned) == sorted(wb.header)
    assert len(owned) == len(set(owned)) == 40
    # The two the brief left out, placed deliberately.
    assert wb.owner_of("Round") == "provenance"
    assert wb.owner_of("Priority_Score") == "protected"
    # And the one it moved: a negotiated commission is Juan's to type.
    assert wb.owner_of("Commission_or_Partner_Terms") == "protected"


def test_a_column_with_no_owner_is_refused(tmp_path, real_config):
    raw = yaml.safe_load((real_config.config_path).read_text(encoding="utf-8"))
    raw["workbook"]["protected_columns"].remove("Material_Sent")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="'Material_Sent' belongs to no ownership list"):
        config_mod.load(path)


def test_a_column_claimed_twice_is_refused(tmp_path, real_config):
    raw = yaml.safe_load((real_config.config_path).read_text(encoding="utf-8"))
    raw["workbook"]["research_columns"].append("Status")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    expected = r"'Status' is claimed by \['research_columns', 'protected_columns'\]"
    with pytest.raises(ValueError, match=expected):
        config_mod.load(path)


# ---------------------------------------------------------------------------
# Column-aware emptiness
# ---------------------------------------------------------------------------


def test_a_cell_that_is_not_an_address_counts_as_empty(real_config):
    spec = real_config.workbook
    junk = "Kai Schweigkofler - Travel Agency Support desk"
    assert spec.is_empty(junk, "Sales_B2B_Email") is True
    assert spec.is_empty("info@jagdhof.at", "Sales_B2B_Email") is False
    # Without a column, the old token-only rule still applies.
    assert spec.is_empty(junk) is False
    # A column with no pattern is unaffected.
    assert spec.is_empty(junk, "Strategic_Fit_Note") is False
    for token in ("", "TBD", "tbd", " n/a ", "FORM-ONLY", "-"):
        assert spec.is_empty(token, "General_Email") is True


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_a_protected_column_is_never_proposed_only_evidenced(real_config):
    """A published commission is evidence for the queue, never a value we write."""
    found = value("Agents earn a commission of 10%", "https://x.example/trade")
    found = found.model_copy(update={"field": Field_.COMMISSION_TERMS})

    chosen, held, _ = choose_values(candidate(), [found], real_config)

    assert Field_.COMMISSION_TERMS not in chosen
    ((_, why),) = _held(held, Field_.COMMISSION_TERMS)
    assert "Commission_or_Partner_Terms is a protected column" in why


def test_a_contacted_row_is_still_filled(real_config):
    """The whole point: being marked YES does not stop the empty cells filling."""
    live = candidate().model_copy(update={"existing": {"general_email": "TBD"}})
    chosen, _, _ = choose_values(live, [value("info@x.example", "https://x.example/")], real_config)
    assert chosen[Field_.GENERAL_EMAIL].value == "info@x.example"


def test_a_cell_holding_a_name_instead_of_an_address_is_proposed_again(real_config):
    """`J270` held a person's name for weeks; the enricher read it as filled."""
    junk = candidate().model_copy(
        update={"existing": {"sales_b2b_email": "Kai Schweigkofler - Travel Agency Support desk"}}
    )
    found = value("trade@jagdhof.at", "https://x.example/trade").model_copy(
        update={"field": Field_.SALES_B2B_EMAIL}
    )
    chosen, _, _ = choose_values(junk, [found], real_config)
    assert chosen[Field_.SALES_B2B_EMAIL].value == "trade@jagdhof.at"

    real = junk.model_copy(update={"existing": {"sales_b2b_email": "desk@jagdhof.at"}})
    chosen, held, _ = choose_values(real, [found], real_config)
    assert Field_.SALES_B2B_EMAIL not in chosen  # a real address is never overwritten
    assert _held(held, Field_.SALES_B2B_EMAIL)


def test_cols_restricts_what_is_proposed_without_losing_the_evidence(real_config):
    values = [
        value("info@x.example", "https://x.example/"),
        value("+41279660303", "https://x.example/", field=Field_.PHONE),
    ]
    chosen, held, _ = choose_values(candidate(), values, real_config, columns={"K"})

    assert Field_.PHONE in chosen and Field_.GENERAL_EMAIL not in chosen
    ((_, why),) = _held(held, Field_.GENERAL_EMAIL)
    assert "outside the columns this run was asked for" in why


# ---------------------------------------------------------------------------
# The flags
# ---------------------------------------------------------------------------


def test_parse_rows_understands_the_contacted_keyword():
    assert parse_rows("contacted") == "contacted"
    assert parse_rows("2:21") == (2, 21)
    assert parse_rows(None) is None
    with pytest.raises(SystemExit, match="or 'contacted'"):
        parse_rows("nonsense")


def test_parse_cols_takes_letters_or_names_and_refuses_protected_ones(real_config):
    assert parse_cols("J,K,L,O", real_config) == {"J", "K", "L", "O"}
    assert parse_cols("Sales_B2B_Email,Phone", real_config) == {"J", "K"}
    assert parse_cols(None, real_config) is None
    with pytest.raises(SystemExit, match="not a PARTNERS column"):
        parse_cols("ZZ", real_config)
    with pytest.raises(SystemExit, match=r"protected column\(s\) \['Contacted'\]"):
        parse_cols("J,Z", real_config)


# ---------------------------------------------------------------------------
# The paste block
# ---------------------------------------------------------------------------


def test_paste_block_uses_the_panel_grammar(real_config):
    from efe.models import CellChange, Confidence, DataClass, RunSummary
    from efe.pipeline import RunOutcome
    from tests.test_noise_control import NOW

    def cell(row, column, old, new, note=""):
        return CellChange(
            row=row,
            column=column,
            field="x",
            entity_id="EFE-0264",
            entity_name="Kempinski Hotel Das Tirol",
            old_value=old,
            new_value=new,
            confidence=Confidence.HIGH,
            data_class=DataClass.CORPORATE_ROLE,
            source_url="https://kempinski.com/contact",
            fetched_at=NOW,
            extractor="tests",
            note=note,
        )

    outcome = RunOutcome()
    outcome.changes = [cell(262, "L", "", "+4353566705"), cell(262, "O", "", "https://li/x")]
    outcome.held = [cell(262, "U", "", "10% commission", "HELD FOR REVIEW - protected | found")]
    summary = RunSummary(
        run_id="20260829-1200",
        round_id="R3-hotels",
        started_at=NOW,
        workbook_in="v10 export",
    )
    provenance = [
        cell(262, "AK", "https://old.example", "https://old.example; https://new.example")
    ]

    block = render_paste_block(
        real_config, summary, outcome, provenance=provenance, handoff=["Autofilter. Select ..."]
    )
    lines = block.splitlines()

    assert "L262   +4353566705" in lines  # a bare value sets the cell
    assert "O262   https://li/x" in lines
    assert "AK262 += ; https://new.example" in lines  # only the delta is appended
    assert "# EFE-0264 Kempinski Hotel Das Tirol - row 262" in lines
    # Everything the run did NOT propose travels as comments, never as cells.
    assert any(line.startswith("#") and "U262" in line for line in lines)
    assert not any(line.startswith("U262") for line in lines)
    assert any("do these in Sheets" in line for line in lines)
    for line in lines:
        assert line.startswith("#") or not line.strip() or line[0].isalpha()


def test_paste_block_writes_a_formula_with_its_sheet_and_equals(real_config):
    from efe.models import CellChange, Confidence, DataClass, RunSummary
    from efe.pipeline import RunOutcome
    from tests.test_noise_control import NOW

    outcome = RunOutcome()
    outcome.changes = [
        CellChange(
            row=30,
            column="B",
            field="x",
            entity_id="",
            entity_name="",
            old_value="",
            new_value="=COUNTA(PARTNERS!$G$2:$G)",
            confidence=Confidence.HIGH,
            data_class=DataClass.CORPORATE_ROLE,
            source_url="https://x.example",
            fetched_at=NOW,
            extractor="tests",
        )
    ]
    summary = RunSummary(run_id="r", round_id="R", started_at=NOW)
    block = render_paste_block(real_config, summary, outcome)
    assert "PARTNERS!B30 = =COUNTA(PARTNERS!$G$2:$G)" in block
