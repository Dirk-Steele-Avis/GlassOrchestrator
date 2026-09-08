"""
Unit tests for handle_complaint_dialog() and _map_damage_type() —
playwright_prototype/steps.py.

Covers:
- _map_damage_type: all three damage label outcomes
- Existing-complaint path: tile found → click → Next → mileage dialog, no new-complaint flow
- New-complaint path: non-WS location selects Side/Rear Window Damage button
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_page_mock():
    """Return a page mock that simulates the existing-complaint dialog state."""
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()
    page.url = "https://app.example.com/work-order"

    # Glass complaint tile — count > 0 signals existing complaint
    tile_first = MagicMock()
    tile_first.click = AsyncMock()

    complaint_filtered = MagicMock()
    complaint_filtered.count = AsyncMock(return_value=1)
    complaint_filtered.first = tile_first

    complaint_locator = MagicMock()
    complaint_locator.filter = MagicMock(return_value=complaint_filtered)

    # Mileage heading — first probe succeeds so mileage_appeared=True
    mileage_first = AsyncMock()
    mileage_first.wait_for = AsyncMock()

    mileage_heading = MagicMock()
    mileage_heading.first = mileage_first

    # Next button
    next_btn = AsyncMock()
    next_btn.click = AsyncMock()

    # Add New Complaint button — must NOT be clicked on the existing path
    add_new_btn_first = AsyncMock()
    add_new_btn_first.click = AsyncMock()
    add_new_locator = MagicMock()
    add_new_locator.first = add_new_btn_first

    def locator_side_effect(selector, **kwargs):
        if "complaintItem" in str(selector):
            return complaint_locator
        if "Add New Complaint" in str(selector) or "Create New Complaint" in str(selector):
            return add_new_locator
        return MagicMock()

    def get_by_role_side_effect(role, **kwargs):
        name = str(kwargs.get("name", ""))
        if role == "button" and "Next" in name:
            return next_btn
        if role == "heading":
            return mileage_heading
        return AsyncMock()

    page.locator = MagicMock(side_effect=locator_side_effect)
    page.get_by_role = MagicMock(side_effect=get_by_role_side_effect)
    page.get_by_text = MagicMock(return_value=MagicMock(first=AsyncMock()))

    return page, tile_first, next_btn, add_new_btn_first


class TestHandleComplaintDialogExistingPath:
    """Existing-complaint path: tile found → click → Next → mileage dialog."""

    def test_existing_tile_is_clicked(self):
        """The glass complaint tile is clicked when it is already present."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, tile_first, next_btn, _ = _make_page_mock()
        asyncio.run(handle_complaint_dialog(page, "59002156", "Glass", "WS", "Replace"))

        tile_first.click.assert_called_once()

    def test_next_button_clicked_after_tile(self):
        """Next is clicked to advance to the mileage dialog after selecting the tile."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, tile_first, next_btn, _ = _make_page_mock()
        asyncio.run(handle_complaint_dialog(page, "59002156", "Glass", "WS", "Replace"))

        next_btn.click.assert_called_once()

    def test_add_new_complaint_not_clicked(self):
        """Add New Complaint must not be triggered when an existing tile is found."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, _, _, add_new_btn_first = _make_page_mock()
        asyncio.run(handle_complaint_dialog(page, "59002156", "Glass", "WS", "Replace"))

        add_new_btn_first.click.assert_not_called()

    def test_does_not_raise(self):
        """Function completes without error when an existing glass complaint is present."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, _, _, _ = _make_page_mock()
        asyncio.run(handle_complaint_dialog(page, "59002156", "Glass", "WS", "Replace"))


class TestMapDamageType:
    """_map_damage_type maps location + action to the correct Compass button label."""

    def test_ws_replace_returns_windshield_crack(self):
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("WS", "Replace") == "Windshield Crack"

    def test_ws_repair_returns_windshield_chip(self):
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("WS", "Repair") == "Windshield Chip"

    def test_non_ws_location_always_returns_side_rear(self):
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("RW", "Replace") == "Side/Rear Window Damage"

    def test_non_ws_repair_still_returns_side_rear(self):
        """Repair on non-WS location is still Side/Rear — repair only valid on windshields."""
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("RW", "Repair") == "Side/Rear Window Damage"

    def test_windshield_long_form_recognised(self):
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("WINDSHIELD", "Replace") == "Windshield Crack"

    def test_case_insensitive_location(self):
        from playwright_prototype.steps import _map_damage_type
        assert _map_damage_type("ws", "replace") == "Windshield Crack"

    def test_rvm_detection_accepts_code_and_sheet_label(self):
        from playwright_prototype.steps import _is_rear_view_mirror
        assert _is_rear_view_mirror("RVM")
        assert _is_rear_view_mirror("Rear View Mirror")


def _make_new_complaint_page_mock():
    """Page mock for the new-complaint path — no existing glass tile."""
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.url = "https://app.example.com/work-order"

    # No existing complaint tile
    empty_filtered = MagicMock()
    empty_filtered.count = AsyncMock(return_value=0)

    complaint_locator = MagicMock()
    complaint_locator.filter = MagicMock(return_value=empty_filtered)

    # Track which XPath locator selectors were requested
    locator_calls = []

    def locator_side_effect(selector, **kwargs):
        locator_calls.append(selector)
        if "complaintItem" in str(selector):
            return complaint_locator
        mock_loc = MagicMock()
        mock_loc.click = AsyncMock()
        mock_loc.first = MagicMock()
        mock_loc.first.click = AsyncMock()
        mock_loc.first.wait_for = AsyncMock(side_effect=Exception("not visible"))
        return mock_loc

    def get_by_role_side_effect(role, **kwargs):
        mock_btn = MagicMock()
        mock_btn.click = AsyncMock()
        mock_btn.first = MagicMock()
        mock_btn.first.wait_for = AsyncMock()
        mock_btn.first.click = AsyncMock()
        return mock_btn

    page.locator = MagicMock(side_effect=locator_side_effect)
    page.get_by_role = MagicMock(side_effect=get_by_role_side_effect)
    page.get_by_text = MagicMock(return_value=MagicMock(first=AsyncMock()))

    return page, locator_calls


class TestHandleComplaintDialogNewPathDamageType:
    """New-complaint path uses the correct damage-type button for non-WS locations."""

    def test_non_ws_location_uses_side_rear_window_selector(self):
        """Side/Rear Window Damage XPath selector is used when location is not WS."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, locator_calls = _make_new_complaint_page_mock()

        with patch("playwright_prototype.steps._click_submit_complaint", new=AsyncMock()), \
             patch("playwright_prototype.steps._wait_for_post_submit_progress", new=AsyncMock(return_value=True)):
            asyncio.run(handle_complaint_dialog(page, "99999999", "Glass", "RW", "Replace"))

        damage_selectors = [s for s in locator_calls if "Side/Rear Window Damage" in str(s)]
        assert damage_selectors, (
            f"Expected XPath selector containing 'Side/Rear Window Damage' but got: {locator_calls}"
        )

    def test_ws_repair_uses_windshield_chip_selector(self):
        """Windshield Chip XPath selector is used for WS + Repair."""
        from playwright_prototype.steps import handle_complaint_dialog

        page, locator_calls = _make_new_complaint_page_mock()

        with patch("playwright_prototype.steps._click_submit_complaint", new=AsyncMock()), \
             patch("playwright_prototype.steps._wait_for_post_submit_progress", new=AsyncMock(return_value=True)):
            asyncio.run(handle_complaint_dialog(page, "99999999", "Glass", "WS", "Repair"))

        damage_selectors = [s for s in locator_calls if "Windshield Chip" in str(s)]
        assert damage_selectors, (
            f"Expected XPath selector containing 'Windshield Chip' but got: {locator_calls}"
        )

    def test_rvm_uses_dedicated_complaint_form(self):
        from playwright_prototype.steps import handle_complaint_dialog

        page, _ = _make_new_complaint_page_mock()
        fill_rvm = AsyncMock()

        with patch("playwright_prototype.steps._fill_rvm_complaint_form", new=fill_rvm), \
             patch("playwright_prototype.steps._click_submit_complaint", new=AsyncMock()), \
             patch("playwright_prototype.steps._wait_for_post_submit_progress", new=AsyncMock(return_value=True)):
            asyncio.run(handle_complaint_dialog(page, "99999999", "Glass", "RVM", "Replace"))

        fill_rvm.assert_awaited_once_with(page, "99999999")


class TestComplaintTypePatterns:
    """COMPLAINT_TYPE_PATTERNS in steps.py matches the correct tile text."""

    def test_glass_pattern_matches_glass(self):
        from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS
        assert COMPLAINT_TYPE_PATTERNS["Glass"].search("Glass Damage")

    def test_glass_pattern_matches_windshield(self):
        from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS
        assert COMPLAINT_TYPE_PATTERNS["Glass"].search("Windshield Crack")

    def test_pm_pattern_matches_pm(self):
        from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS
        assert COMPLAINT_TYPE_PATTERNS["PM"].search("PM Gas")

    def test_glass_pattern_does_not_match_pm(self):
        from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS
        assert not COMPLAINT_TYPE_PATTERNS["Glass"].search("PM preventive maintenance")

    def test_check_existing_work_item_importable(self):
        """Renamed function must be importable from steps."""
        from playwright_prototype.steps import check_existing_work_item
        assert callable(check_existing_work_item)

    def test_select_opcode_importable(self):
        """Renamed function must be importable from steps."""
        from playwright_prototype.steps import select_opcode
        assert callable(select_opcode)
