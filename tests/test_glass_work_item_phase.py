"""
Unit Tests for Phase 7 core logic.

GLAS-1: check_existing_work_item() — open item detection
GLAS-2: read_glass_claims() — sheet reader filtering
GLAS-3: run_glass_work_item_phase() — runner loop behaviour
"""

import pytest
from unittest.mock import MagicMock, patch, call


# ─── GLAS-1: check_existing_work_item() ──────────────────────────────────────


class TestGLAS1_CheckExistingWorkItem:
    """check_existing_work_item() returns True if an open glass work item exists."""

    def _make_tile(self, text: str) -> MagicMock:
        tile = MagicMock()
        tile.text = text
        return tile

    def test_returns_true_when_open_glass_tile_found(self):
        """Returns True when get_work_items yields a tile containing 'glass'."""
        from flows.work_item_flow import check_existing_work_item
        driver = MagicMock()
        tiles = [self._make_tile("Glass Damage - Open")]
        with patch("flows.work_item_flow.get_work_items", return_value=tiles):
            assert check_existing_work_item(driver, "12345678", "GLASS") is True

    def test_returns_false_when_no_tiles(self):
        """Returns False when no open work items exist."""
        from flows.work_item_flow import check_existing_work_item
        driver = MagicMock()
        with patch("flows.work_item_flow.get_work_items", return_value=[]):
            assert check_existing_work_item(driver, "12345678", "GLASS") is False

    def test_returns_false_when_no_glass_tile_among_others(self):
        """Returns False when open tiles exist but none match GLASS."""
        from flows.work_item_flow import check_existing_work_item
        driver = MagicMock()
        tiles = [self._make_tile("PM - Open"), self._make_tile("Brake - Open")]
        with patch("flows.work_item_flow.get_work_items", return_value=tiles):
            assert check_existing_work_item(driver, "12345678", "GLASS") is False

    def test_raises_on_exception(self):
        """Re-raises when get_work_items throws so the runner can mark MVA as failed."""
        from flows.work_item_flow import check_existing_work_item
        driver = MagicMock()
        with patch("flows.work_item_flow.get_work_items", side_effect=Exception("boom")):
            with pytest.raises(Exception, match="boom"):
                check_existing_work_item(driver, "12345678", "GLASS")

    def test_case_insensitive_match(self):
        """Keyword match is case-insensitive."""
        from flows.work_item_flow import check_existing_work_item
        driver = MagicMock()
        tiles = [self._make_tile("GLASS REPLACEMENT - Open")]
        with patch("flows.work_item_flow.get_work_items", return_value=tiles):
            assert check_existing_work_item(driver, "12345678", "glass") is True


# ─── GLAS-2: read_glass_claims() ─────────────────────────────────────────────


class TestGLAS2_ReadGlassClaims:
    """read_glass_claims() filters sheet rows correctly."""

    def _make_ws(self, rows: list[dict]) -> MagicMock:
        ws = MagicMock()
        ws.get_all_records.return_value = rows
        return ws

    def test_returns_eligible_unprocessed_rows(self):
        """Returns Replacement rows where WorkItemCreated is blank."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": "Windshield"},
        ])
        result = read_glass_claims(ws)
        assert len(result) == 1
        assert result[0]["mva"] == "11111111"

    def test_excludes_repair_rows(self):
        """Repair rows are not eligible and must be excluded."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Repair", "WorkItemCreated": "", "Location": ""},
        ])
        assert read_glass_claims(ws) == []

    def test_excludes_already_processed_rows(self):
        """Rows where WorkItemCreated is 'Y' are skipped."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "Y", "Location": ""},
        ])
        assert read_glass_claims(ws) == []

    def test_defaults_location_to_windshield_when_blank(self):
        """Blank Location column defaults to WINDSHIELD."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": ""},
        ])
        result = read_glass_claims(ws)
        assert result[0]["location"] == "WINDSHIELD"

    def test_preserves_explicit_location(self):
        """Explicit Location value is preserved."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": "Side"},
        ])
        result = read_glass_claims(ws)
        assert result[0]["location"] == "Side"

    def test_uses_rear_view_mirror_area_when_location_is_blank(self):
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {
                "MVA": "11111111",
                "Damage Type": "Replacement",
                "WorkItemCreated": "",
                "Location": "",
                "Area": "Rear View Mirror",
            },
        ])
        result = read_glass_claims(ws)
        assert result[0]["location"] == "Rear View Mirror"

    def test_returns_correct_keys(self):
        """Each result dict has exactly mva, damage_type, location keys."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": ""},
        ])
        result = read_glass_claims(ws)
        assert set(result[0].keys()) == {"mva", "damage_type", "location"}

    def test_empty_sheet_returns_empty_list(self):
        """No rows in sheet returns empty list."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([])
        assert read_glass_claims(ws) == []

    def test_mixed_rows_returns_only_eligible(self):
        """Only unprocessed Replacement rows are returned."""
        from flows.glass_work_item_phase import read_glass_claims
        ws = self._make_ws([
            {"MVA": "11111111", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": ""},
            {"MVA": "22222222", "Damage Type": "Repair", "WorkItemCreated": "", "Location": ""},
            {"MVA": "33333333", "Damage Type": "Replacement", "WorkItemCreated": "Y", "Location": ""},
            {"MVA": "44444444", "Damage Type": "Replacement", "WorkItemCreated": "", "Location": ""},
        ])
        result = read_glass_claims(ws)
        assert len(result) == 2
        assert {r["mva"] for r in result} == {"11111111", "44444444"}


# ─── GLAS-3: run_glass_work_item_phase() ─────────────────────────────────────


class TestGLAS3_RunGlassWorkItemPhase:
    """run_glass_work_item_phase() runner loop behaviour."""

    def _manifest(self, mvas: list[str]) -> list[dict]:
        return [{"mva": m, "damage_type": "Replacement", "location": "WINDSHIELD"} for m in mvas]

    def _base_patches(self):
        """Return context managers that stub navigation for all loop tests."""
        return (
            patch("flows.glass_work_item_phase.warmup_compass", return_value=True),
            patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True),
        )

    def test_returns_summary_dict(self):
        """Always returns dict with processed/created/skipped/failed keys."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=True):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111"]))
        assert set(result.keys()) == {"processed", "created", "skipped", "failed"}

    def test_skips_mva_with_existing_work_item(self):
        """Increments skipped when work item already exists."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=True):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111"]))
        assert result["skipped"] == 1
        assert result["created"] == 0

    def test_creates_work_item_when_none_exists(self):
        """Increments created when handler succeeds."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        mock_handler = MagicMock()
        mock_handler.create_work_item.return_value = {"status": "created", "mva": "11111111"}
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111"]))
        assert result["created"] == 1
        assert result["skipped"] == 0

    def test_increments_failed_on_handler_error(self):
        """Increments failed when handler returns non-created status."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        mock_handler = MagicMock()
        mock_handler.create_work_item.return_value = {"status": "failed", "mva": "11111111"}
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111"]))
        assert result["failed"] == 1
        assert result["created"] == 0

    def test_navigation_failure_increments_failed_skips_check_and_create(self):
        """navigate_to_mva returning False increments failed; check and create not called."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        mock_check = MagicMock()
        mock_handler = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=False), \
             patch("flows.glass_work_item_phase.check_existing_work_item", mock_check), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111"]))
        assert result["failed"] == 1
        assert result["created"] == 0
        mock_check.assert_not_called()
        mock_handler.create_work_item.assert_not_called()

    def test_continues_after_exception(self):
        """Loop never aborts — all MVAs attempted even after an exception."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", side_effect=Exception("boom")):
            result = run_glass_work_item_phase(driver, self._manifest(["11111111", "22222222"]))
        assert result["processed"] == 2
        assert result["failed"] == 2

    def test_empty_manifest_returns_zero_counts(self):
        """Empty manifest returns all-zero summary without calling navigation."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True):
            result = run_glass_work_item_phase(driver, [])
        assert result == {"processed": 0, "created": 0, "skipped": 0, "failed": 0}

    def test_marks_work_item_created_in_sheet_on_success(self):
        """Calls sheet_client to mark WorkItemCreated=Y after successful creation."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        mock_handler = MagicMock()
        mock_handler.create_work_item.return_value = {"status": "created", "mva": "11111111"}
        mock_sheet_client = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            run_glass_work_item_phase(driver, self._manifest(["11111111"]),
                                      sheet_client=mock_sheet_client, tab_name="GlassClaims")
        mock_sheet_client.mark_work_item_created.assert_called_once_with("11111111", "GlassClaims")

    def test_does_not_call_sheet_when_no_client(self):
        """No sheet_client — no sheet calls made."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        mock_handler = MagicMock()
        mock_handler.create_work_item.return_value = {"status": "created", "mva": "11111111"}
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            run_glass_work_item_phase(driver, self._manifest(["11111111"]), sheet_client=None)

    def test_processed_count_equals_manifest_size(self):
        """processed always equals the number of entries in the manifest."""
        from flows.glass_work_item_phase import run_glass_work_item_phase
        driver = MagicMock()
        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=True):
            result = run_glass_work_item_phase(driver, self._manifest(["1", "2", "3"]))
        assert result["processed"] == 3
