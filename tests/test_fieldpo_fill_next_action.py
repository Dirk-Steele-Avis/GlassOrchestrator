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


def test_extract_fpo_skips_other_creator_and_selects_topmost_steele_po():
    text = _po_section(
        _po_card("FPO1178731", "Glass Calibration", "Created by Other, User on 2026-10-19"),
        _po_card("FPO1178730", "Glass Parts and Install", "Created by Steele, Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178730"]


def test_extract_fpo_selects_topmost_when_multiple_steele_pos():
    text = _po_section(
        _po_card("FPO1178731", "Glass Calibration", "Created by Steele, Dirk on 2026-10-19"),
        _po_card("FPO1178730", "Glass Parts and Install", "Created by Steele, Dirk on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178731"]


def test_extract_fpo_creator_match_is_case_insensitive():
    text = _po_section(
        _po_card("FPO1178733", "Glass Parts and Install", "Created by STEELE, DIRK on 2026-10-19"),
    )

    assert fieldpo._extract_fpo_values_from_text(text) == ["FPO1178733"]