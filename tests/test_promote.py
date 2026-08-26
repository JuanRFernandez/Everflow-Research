"""Promotion: candidate CSV rows become PARTNERS rows, through the fidelity gate."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from openpyxl import load_workbook

from efe.cli import main
from efe.models import VerificationError
from efe.workbook.promote import plan_promotion, read_candidates, write_promoted
from efe.workbook.reader import load_workbook_view
from efe.workbook.state import load_state, state_path
from efe.workbook.verify import compare, snapshot
from tests.conftest import PARTNERS_HEADERS


def _csv(path: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> Path:
    columns = columns or PARTNERS_HEADERS[:39]  # the discovery CSV predates Material_Sent
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


def _candidate(entity_id: str, name: str, website: str, **extra) -> dict[str, str]:
    row = {
        "ID": entity_id,
        "Entity_Name": name,
        "Category": "1. Hotels",
        "Resort_Base": "Seefeld",
        "Region_Valley": "Tirol",
        "Country": "AT",
        "Website_URL": website,
        "General_Email": "TBD",
        "Contacted": "NO",
        "Follow_Up_Days": "14",
        "Status": "Not started",
        "Next_Action": "TBD",
        "Source_URL": "https://www.seefeld.com/de/hotels.html",
        "Date_Verified": "2026-08-25",
        "Round": "R3-discovery",
        "Strategic_Fit_Note": "[CANDIDATO] seefeld.com - 4*S",
    }
    row.update(extra)
    return row


def test_plan_keeps_new_rows_and_explains_every_rejection(synthetic_config, tmp_path):
    view = load_workbook_view(synthetic_config)
    source = _csv(
        tmp_path / "cands.csv",
        [
            _candidate("EFE-0359", "Hotel Seespitz", "https://seespitz.at"),
            _candidate("EFE-0360", "Summit Lodge Verbier", "https://other.example"),  # name exists
            _candidate(
                "EFE-0361", "Grandclass Twin", "https://grandclass.example"
            ),  # domain exists
            _candidate("EFE-0001", "Fresh Name", "https://fresh.example"),  # ID exists
            _candidate("EFE-0362", "Hotel Kristall", "https://hotel-kristall.at"),
            _candidate("EFE-0362", "Hotel Kristall Bis", "https://kristall-bis.at"),  # ID repeated
            _candidate("EFE-0363", "Hotel Seespitz", "https://seespitz-two.at"),  # name repeated
        ],
    )
    plan = plan_promotion(view, synthetic_config, read_candidates(source, synthetic_config), source)

    assert [r for r, _ in plan.accepted] == [10, 11]  # data ends at row 9
    assert plan.ids == ["EFE-0359", "EFE-0362"]
    reasons = {entity_id: reason for entity_id, _, reason in plan.rejected}
    assert "name already in PARTNERS (row 2)" in reasons["EFE-0360"]
    assert "domain grandclass.example already in PARTNERS" in reasons["EFE-0361"]
    assert "ID EFE-0001 already in PARTNERS" in reasons["EFE-0001"]
    assert "repeated in the CSV" in reasons["EFE-0363"]
    assert sum(1 for i, _, r in plan.rejected if i == "EFE-0362") == 1
    assert "skipped : EFE-0001" in plan.describe()


def test_unknown_csv_column_is_refused(synthetic_config, tmp_path):
    source = _csv(
        tmp_path / "bad.csv",
        [_candidate("EFE-0359", "X", "https://x.at")],
        ["ID", "Entity_Name", "Bogus"],
    )
    with pytest.raises(VerificationError, match="not PARTNERS columns"):
        read_candidates(source, synthetic_config)


def test_promoted_rows_land_after_the_data_with_the_live_formula(synthetic_config, tmp_path):
    view = load_workbook_view(synthetic_config)
    source = _csv(
        tmp_path / "cands.csv",
        [
            _candidate("EFE-0359", "Hotel Seespitz", "https://seespitz.at"),
            _candidate(
                "EFE-0360",
                "Hotel Kristall",
                "https://hotel-kristall.at",
                Capacity_Keys_or_Beds="120",
            ),
        ],
    )
    plan = plan_promotion(view, synthetic_config, read_candidates(source, synthetic_config), source)
    out = write_promoted(synthetic_config, view, plan, run_id="test-promote")
    assert out.name.endswith("_v02.xlsx")

    wb = load_workbook(out)
    try:
        ws = wb["PARTNERS"]
        assert ws["A10"].value == "EFE-0359" and ws["B10"].value == "Hotel Seespitz"
        assert ws["A11"].value == "EFE-0360" and ws["S11"].value == 120  # integers become numbers
        assert ws["AC10"].value == '=IFERROR(AA10+AB10,"")'
        assert ws["AC11"].value == '=IFERROR(AA11+AB11,"")'
        assert ws["AB10"].value == 14 and ws["Z10"].value == "NO"
        assert ws["AN10"].value is None  # Material_Sent: not in the CSV, left blank
        assert ws["AM10"].value == "R3-discovery"
        # existing rows untouched
        for row in range(2, 10):
            assert ws.cell(row, 1).value == f"EFE-{row - 1:04d}"
        log = wb["CHANGELOG"]
        assert log["A5"].value == "v02" and "2 candidate rows appended" in log["C5"].value
        assert log["D5"].value == "efe promote"
        detail = wb["CHANGELOG_DETAIL"]
        assert detail["B3"].value == "test-promote" and detail["G3"].value == "row"
        assert detail["D4"].value == "EFE-0360"
    finally:
        wb.close()

    # The output is faithful to the input everywhere else.
    before, after = snapshot(view.path), snapshot(out)
    partners = {k for k in set(before.values) | set(after.values) if k[0] == "PARTNERS"}
    logs = {
        k
        for k in after.values
        if k[0] in ("CHANGELOG", "CHANGELOG_DETAIL") and k not in before.values
    }
    assert (
        compare(
            before,
            after,
            allowed_value_changes=partners | logs,
            allowed_autofilter_changes={"CHANGELOG_DETAIL"},
        )
        == []
    )

    # ... and the resolver reads it next, 2 rows richer, not behind the baseline.
    synthetic_config.output_dir = None
    moved = Path(out).replace(synthetic_config.workbook_directory / out.name)
    nxt = load_workbook_view(synthetic_config)
    assert nxt.path == moved and nxt.version == 2 and nxt.data_rows == 10


def test_nothing_to_promote_is_refused(synthetic_config, tmp_path):
    view = load_workbook_view(synthetic_config)
    source = _csv(
        tmp_path / "dupes.csv",
        [_candidate("EFE-0001", "Summit Lodge Verbier", "https://summitlodge.example")],
    )
    plan = plan_promotion(view, synthetic_config, read_candidates(source, synthetic_config), source)
    with pytest.raises(VerificationError, match="nothing to promote"):
        write_promoted(synthetic_config, view, plan, run_id="test-none")
    assert not list(synthetic_config.output_directory.glob("*.xlsx"))


def test_cli_promote_dry_run_then_write(synthetic_config, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    source = _csv(
        tmp_path / "cands.csv",
        [
            _candidate("EFE-0359", "Hotel Seespitz", "https://seespitz.at"),
            _candidate("EFE-0001", "Fresh Name", "https://fresh.example"),
        ],
    )
    rc = main(["--config", str(synthetic_config.config_path), "promote", str(source), "--dry-run"])
    text = capsys.readouterr().out
    assert rc == 0, text
    assert "1 row(s) to append" in text and "EFE-0001" in text and "Dry run" in text
    assert not list(synthetic_config.output_directory.glob("*.xlsx"))

    rc = main(["--config", str(synthetic_config.config_path), "promote", str(source)])
    text = capsys.readouterr().out
    assert rc == 0, text
    written = list(synthetic_config.output_directory.glob("*_v02.xlsx"))
    assert len(written) == 1
    saved = load_state(state_path(synthetic_config.state_directory))
    assert saved is not None and saved.version == 2 and saved.data_rows == 9
    assert saved.command == "efe promote (output)"
    assert saved.file_sha256  # re-fingerprinted in the ledger
