from datetime import date
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import FieldPOFillNextAction as fieldpo


HEADERS = ["Inventory Date", "MVA", "Action", "NextAction"]


def test_fieldpo_login_reaches_dashboard(monkeypatch):
    page = MagicMock()
    popup = MagicMock()
    monkeypatch.setattr(fieldpo, "_dismiss_attention_popup", popup)

    fieldpo._ensure_fieldpo_login(page)

    page.goto.assert_called_once_with(fieldpo.FIELDPO_URL)
    popup.assert_called_once_with(page)


def test_fieldpo_login_expired_stops_without_prompt(monkeypatch):
    page = MagicMock()
    page.wait_for_url.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded.")
    prompt = MagicMock()
    monkeypatch.setattr("builtins.input", prompt)

    with pytest.raises(PlaywrightTimeoutError):
        fieldpo._ensure_fieldpo_login(page)

    prompt.assert_not_called()
    page.goto.assert_called_once_with(fieldpo.FIELDPO_URL)


def test_collect_candidates_uses_explicit_inventory_date():
    values = [
        HEADERS,
        ["09/24/2026", "11111111", "Replace(AGN)", ""],
        ["09/25/2026", "22222222", "Replace(AGN)", ""],
    ]

    candidates, next_action_column = fieldpo._collect_candidates(
        values,
        date(2026, 9, 25),
    )

    assert candidates == [(3, "22222222")]
    assert next_action_column == 4


def test_collect_verification_rows_uses_explicit_inventory_date():
    values = [
        HEADERS,
        ["09/24/2026", "11111111", "Replace(AGN)", "FPO1"],
        ["09/25/2026", "22222222", "Replace(AGN)", "FPO2"],
    ]

    rows = fieldpo._collect_non_blank_verification_rows(
        values,
        4,
        date(2026, 9, 25),
    )

    assert rows == [(3, "22222222", "FPO2")]


def test_collect_blank_guard_rows_uses_explicit_inventory_date():
    values = [
        HEADERS,
        ["09/24/2026", "11111111", "Replace(AGN)", ""],
        ["09/25/2026", "22222222", "Replace(AGN)", ""],
    ]

    rows = fieldpo._collect_in_scope_blank_rows(
        values,
        4,
        date(2026, 9, 25),
    )

    assert rows == [(3, "22222222")]


def _po_section(*cards: str) -> str:
    return f"Purchase Orders [{len(cards)}]\n" + "\n".join(cards)


def _po_card(po: str, description: str, creator_line: str) -> str:
    return f"PO# {po}\n{description}\nAGN GLASS LLC\n{creator_line}"


def test_extract_fpo_counts_po_created_by_steele_dirk():
    text = _po_section(
        _po_card("FPO1178730", "Glass Parts and Install", "Created by Steele, Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178730"]


def test_extract_fpo_excludes_po_created_by_other_user():
    text = _po_section(
        _po_card("FPO1178731", "Glass Parts and Install", "Created by Other, User on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == []


def test_extract_fpo_excludes_po_without_creator_line():
    text = _po_section(
        _po_card("FPO1178732", "Glass Parts and Install", "No creator shown"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == []


def test_extract_fpo_skips_other_creator_po():
    text = _po_section(
        _po_card("FPO1178731", "Glass Calibration", "Created by Other, User on 2026-10-19"),
        _po_card("FPO1178730", "Glass Parts and Install", "Created by Steele, Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178730"]


def test_extract_fpo_returns_every_steele_po_for_the_repair():
    text = _po_section(
        _po_card("FPO1178731", "Glass Calibration", "Created by Steele, Dirk on 2026-10-19"),
        _po_card("FPO1178730", "Glass Parts and Install", "Created by Steele, Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178731", "FPO1178730"]


def test_extract_fpo_creator_match_is_case_insensitive():
    text = _po_section(
        _po_card("FPO1178733", "Glass Parts and Install", "Created by STEELE, DIRK on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178733"]

def test_extract_fpo_creator_match_ignores_spacing():
    text = _po_section(
        _po_card("FPO1178734", "Parts/Labor", "Created by Steele,Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178734"]


def test_extract_fpo_excludes_po_created_before_inventory_date():
    text = _po_section(
        _po_card("FPO1000001", "Parts/Labor", "Created by Steele, Dirk on 2026-03-02"),
        _po_card("FPO1178730", "Parts/Labor", "Created by Steele, Dirk on 2026-10-19"),
        _po_card("FPO1178731", "Camera Calibration", "Created by Steele, Dirk on 2026-10-20"),
    )

    assert fieldpo._extract_fpo_values_from_text(text, date(2026, 10, 19)) == [
        "FPO1178730",
        "FPO1178731",
    ]


def test_extract_fpo_all_old_pos_yield_nothing():
    text = _po_section(
        _po_card("FPO1000001", "Parts/Labor", "Created by Steele, Dirk on 2026-03-02"),
    )

    assert fieldpo._extract_fpo_values_from_text(text, date(2026, 10, 19)) == []


@pytest.mark.parametrize(
    ("result_texts", "expected"),
    [
        (["MVA# 061093642\nWO# 1"], 0),
        (["MVA# 61093642\nWO# 1"], 0),
        (["MVA# 106109364\nWO# 1", "MVA# 061093642\nWO# 2"], 1),
        (["MVA# 106109364\nWO# 1"], None),
        ([], None),
    ],
)
def test_select_exact_mva_result_ignores_other_vehicles(result_texts, expected):
    assert fieldpo._select_exact_mva_result(result_texts, "61093642") == expected


def test_extract_vin_requires_exactly_one_vin():
    assert fieldpo._extract_vin_from_text("VIN: 1C4SJSBP6SS537975") == "1C4SJSBP6SS537975"
    assert fieldpo._extract_vin_from_text("no vin here") == ""
    assert (
        fieldpo._extract_vin_from_text("VIN 1C4SJSBP6SS537975\nVIN 1FMDE7BH9TLA47847") == ""
    )


def _resolve_with(monkeypatch, *, fieldpo_vin, pos):
    monkeypatch.setattr(fieldpo, "_open_vehicle", MagicMock(return_value=fieldpo_vin))
    monkeypatch.setattr(fieldpo, "_go_to_active_work_order_tab", MagicMock())
    extract = MagicMock(return_value=pos)
    monkeypatch.setattr(fieldpo, "_extract_fpo_values", extract)
    return extract


def test_resolve_writes_review_when_fieldpo_vin_differs(monkeypatch):
    extract = _resolve_with(monkeypatch, fieldpo_vin="1FMDE7BH9TLA47847", pos=["FPO1178730"])

    result = fieldpo._resolve_next_action(
        MagicMock(), "061093642", "1C4SJSBP6SS537975", date(2026, 10, 19)
    )

    assert result == "review"
    extract.assert_not_called()


def test_resolve_writes_review_when_sheet_vin_invalid(monkeypatch):
    _resolve_with(monkeypatch, fieldpo_vin="1C4SJSBP6SS537975", pos=["FPO1178730"])

    result = fieldpo._resolve_next_action(MagicMock(), "061093642", "N/A", date(2026, 10, 19))

    assert result == "review"
    fieldpo._open_vehicle.assert_not_called()


def test_resolve_writes_missing_when_no_exact_mva(monkeypatch):
    _resolve_with(monkeypatch, fieldpo_vin="", pos=["FPO1178730"])

    result = fieldpo._resolve_next_action(
        MagicMock(), "061093642", "1C4SJSBP6SS537975", date(2026, 10, 19)
    )

    assert result == "missing"


def test_resolve_lists_all_eligible_pos_using_inventory_date(monkeypatch):
    extract = _resolve_with(
        monkeypatch, fieldpo_vin="1C4SJSBP6SS537975", pos=["FPO1178730", "FPO1178731"]
    )

    result = fieldpo._resolve_next_action(
        MagicMock(), "061093642", "1c4sjsbp6ss537975", date(2026, 10, 19)
    )

    assert result == "FPO1178730, FPO1178731"
    assert extract.call_args.args[1] == date(2026, 10, 19)


@pytest.mark.parametrize(
    ("existing", "resolved", "needs_review"),
    [
        ("FPO1178730", "FPO1178730", False),
        ("FPO1178730", "FPO1178730, FPO1178731", False),
        ("fpo1178730", "FPO1178730", False),
        ("FPO1178730", "FPO1178799", True),
        ("FPO1178730", "missing", True),
        ("FPO1178730", "review", True),
        ("need pic", "FPO1178730", False),
        ("need pic", "review", False),
    ],
)
def test_verification_flags_only_contradicted_po_values(existing, resolved, needs_review):
    assert fieldpo._verification_needs_review(existing, resolved) is needs_review
