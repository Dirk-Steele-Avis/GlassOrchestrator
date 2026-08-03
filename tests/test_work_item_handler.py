"""
Unit Tests for WorkItemHandler / GlassWorkItemHandler refactor (Phase 7).

WIH-1: detect_complaints() delegation
WIH-2: should_handle_existing_complaint() keyword matching
WIH-3: map_damage_type_to_ui() damage/location matrix
WIH-4: WorkItemConfig normalisation
WIH-5: create_work_item_handler() factory
"""

import pytest
from unittest.mock import MagicMock, patch

from flows.work_item_handler import (
    GlassWorkItemHandler,
    WorkItemConfig,
    create_work_item_handler,
)
from core.complaint_types import GlassDamageType


# ─── WIH-1: detect_complaints() delegation ───────────────────────────────────


class TestWIH1_DetectComplaints:
    """detect_complaints() on GlassWorkItemHandler must delegate to
    detect_glass_complaints() from complaints_flows, not call the old
    detect_existing_complaints() directly."""

    def test_detect_complaints_delegates_to_detect_glass_complaints(self):
        """GlassWorkItemHandler.detect_complaints() calls detect_glass_complaints()."""
        mock_driver = MagicMock()
        handler = GlassWorkItemHandler(mock_driver)

        mock_result = [MagicMock(), MagicMock()]
        # Patch the source module (flows.complaints_flows) rather than the
        # import site in work_item_handler, because detect_glass_complaints is
        # imported inside the method body (deferred import), so the name lives
        # only in the complaints_flows namespace at call time.
        with patch(
            "flows.complaints_flows.detect_glass_complaints",
            return_value=mock_result,
        ) as mock_fn:
            result = handler.detect_complaints(mock_driver)

        mock_fn.assert_called_once_with(mock_driver, mva=None)
        assert result == mock_result

    def test_detect_complaints_returns_list(self):
        """detect_complaints() always returns a list (not None)."""
        mock_driver = MagicMock()
        handler = GlassWorkItemHandler(mock_driver)

        with patch(
            "flows.complaints_flows.detect_glass_complaints",
            return_value=[],
        ):
            result = handler.detect_complaints(mock_driver)

        assert isinstance(result, list)

    def test_no_complaints_calls_create_new_complaint(self):
        """When detect_complaints returns [], create_new_complaint should be called."""
        mock_driver = MagicMock()
        handler = GlassWorkItemHandler(mock_driver)
        config = WorkItemConfig(mva="12345678", damage_type="REPLACEMENT")
        with patch("flows.complaints_flows.detect_glass_complaints", return_value=[]):
            with patch.object(handler, "_click_add_work_item_button", return_value=True):
                with patch.object(handler, "create_new_complaint", return_value={"status": "created"}) as mock_create:
                    handler.create_work_item(config)
        mock_create.assert_called_once_with(config)


# ─── WIH-2: should_handle_existing_complaint() ───────────────────────────────


class TestWIH2_ShouldHandleExistingComplaint:
    """Keyword matching for glass-related complaint text."""

    def setup_method(self):
        self.handler = GlassWorkItemHandler(MagicMock())

    @pytest.mark.parametrize(
        "text",
        ["Open\nGlass repair needed", "Open\nWindshield broken", "Open\nCrack on front", "Open\nChip detected", "Open\nReplace window"],
    )
    def test_returns_true_for_open_glass_complaints(self, text):
        assert self.handler.should_handle_existing_complaint(text) is True

    @pytest.mark.parametrize(
        "text",
        ["Closed\nGlass repair needed", "Completed\nWindshield broken", "Open\nOil change due", "Open\nBrake pad replacement"],
    )
    def test_returns_false_for_non_open_or_non_glass_complaints(self, text):
        assert self.handler.should_handle_existing_complaint(text) is False


# ─── WIH-3: map_damage_type_to_ui() matrix ───────────────────────────────────


class TestWIH3_MapDamageTypeToUi:
    """Full damage-type / location → GlassDamageType.value matrix."""

    def setup_method(self):
        self.handler = GlassWorkItemHandler(MagicMock())

    def test_repair_windshield_returns_windshield_chip(self):
        result = self.handler.map_damage_type_to_ui("REPAIR", "WINDSHIELD")
        assert result == GlassDamageType.WINDSHIELD_CHIP.value

    def test_repair_side_returns_side_rear(self):
        result = self.handler.map_damage_type_to_ui("REPAIR", "SIDE")
        assert result == GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value

    def test_repair_rear_returns_side_rear(self):
        result = self.handler.map_damage_type_to_ui("REPAIR", "REAR")
        assert result == GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value

    def test_repair_none_location_returns_side_rear(self):
        result = self.handler.map_damage_type_to_ui("REPAIR", None)
        assert result == GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value

    def test_replacement_windshield_returns_windshield_crack(self):
        result = self.handler.map_damage_type_to_ui("REPLACEMENT", "WINDSHIELD")
        assert result == GlassDamageType.WINDSHIELD_CRACK.value

    def test_replacement_side_returns_side_rear(self):
        result = self.handler.map_damage_type_to_ui("REPLACEMENT", "SIDE")
        assert result == GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value

    def test_replacement_rear_returns_side_rear(self):
        result = self.handler.map_damage_type_to_ui("REPLACEMENT", "REAR")
        assert result == GlassDamageType.SIDE_REAR_WINDOW_DAMAGE.value

    def test_replacement_none_location_returns_unknown(self):
        result = self.handler.map_damage_type_to_ui("REPLACEMENT", None)
        assert result == GlassDamageType.UNKNOWN.value

    def test_none_damage_type_none_location_returns_unknown(self):
        result = self.handler.map_damage_type_to_ui(None, None)
        assert result == GlassDamageType.UNKNOWN.value


# ─── WIH-4: WorkItemConfig normalisation ─────────────────────────────────────


class TestWIH4_WorkItemConfigNormalisation:
    """WorkItemConfig.__post_init__ strips whitespace from mva and
    uppercases damage_type / location."""

    def test_mva_whitespace_is_stripped(self):
        cfg = WorkItemConfig(mva="  12345678  ")
        assert cfg.mva == "12345678"

    def test_damage_type_is_uppercased(self):
        cfg = WorkItemConfig(mva="12345678", damage_type="repair")
        assert cfg.damage_type == "REPAIR"

    def test_damage_type_whitespace_is_stripped(self):
        cfg = WorkItemConfig(mva="12345678", damage_type="  repair  ")
        assert cfg.damage_type == "REPAIR"

    def test_location_is_uppercased(self):
        cfg = WorkItemConfig(mva="12345678", location="windshield")
        assert cfg.location == "WINDSHIELD"

    def test_location_whitespace_is_stripped(self):
        cfg = WorkItemConfig(mva="12345678", location="  side  ")
        assert cfg.location == "SIDE"

    def test_none_damage_type_stays_none(self):
        cfg = WorkItemConfig(mva="12345678")
        assert cfg.damage_type is None

    def test_none_location_stays_none(self):
        cfg = WorkItemConfig(mva="12345678")
        assert cfg.location is None


# ─── WIH-5: create_work_item_handler() factory ───────────────────────────────


class TestWIH5_Factory:
    """Factory returns correct handler type and raises on unknown types."""

    def test_glass_type_returns_glass_handler(self):
        mock_driver = MagicMock()
        handler = create_work_item_handler("GLASS", mock_driver)
        assert isinstance(handler, GlassWorkItemHandler)

    def test_glass_type_lowercase_returns_glass_handler(self):
        mock_driver = MagicMock()
        handler = create_work_item_handler("glass", mock_driver)
        assert isinstance(handler, GlassWorkItemHandler)

    def test_unknown_type_raises_value_error(self):
        mock_driver = MagicMock()
        with pytest.raises(ValueError):
            create_work_item_handler("UNKNOWN", mock_driver)

    def test_handler_receives_driver(self):
        mock_driver = MagicMock()
        handler = create_work_item_handler("GLASS", mock_driver)
        assert handler.driver is mock_driver


# ─── WIH-6: create_work_item() stores _current_mva ──────────────────────────


class TestWIH6_CreateWorkItemStoresMva:
    """create_work_item() must store self._current_mva at the start."""

    def test_current_mva_is_set_before_click(self):
        """_current_mva should be set even when _click_add_work_item_button fails."""
        mock_driver = MagicMock()
        handler = GlassWorkItemHandler(mock_driver)

        config = WorkItemConfig(mva="12345678", damage_type="REPLACEMENT")

        # Stub out button click to fail fast so we don't need full UI
        with patch.object(handler, "_click_add_work_item_button", return_value=False):
            handler.create_work_item(config)

        assert handler._current_mva == "12345678"
