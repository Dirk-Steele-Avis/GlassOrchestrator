"""
Unit tests for navigate_to_mva() fail-fast behavior in playwright_prototype/steps.py.

Focus:
- invalid compass_vehicle_url_template must fail immediately with diagnostics
- valid compass_vehicle_url_template performs direct navigation
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestNavigateToMvaFailFast:
    """URL-template handling should be explicit and fail-fast."""

    def test_invalid_vehicle_url_template_raises_runtime_error(self):
        """Bad format templates must not silently fall back to MVA entry flow."""
        from playwright_prototype.steps import navigate_to_mva

        page = MagicMock()
        page.url = "https://avisbudget.palantirfoundry.com/workspace/fleet-operations-pwa/health"
        page.goto = AsyncMock()
        add_button = MagicMock()
        add_button.filter.return_value.wait_for = AsyncMock()
        page.locator.return_value = add_button

        with patch("playwright_prototype.steps.get_config", return_value="https://example.com/{oops}"), \
             patch("playwright_prototype.steps._open_vehicle_search_context", new=AsyncMock(return_value=page)), \
               patch("playwright_prototype.steps._enter_mva", new=AsyncMock()) as mock_enter_mva, \
                             patch("playwright_prototype.steps._wait_for_open_work_items_tab_ready", new=AsyncMock()) as mock_ready:
            with pytest.raises(RuntimeError, match="invalid compass_vehicle_url_template"):
                asyncio.run(navigate_to_mva(page, "59000001"))

        mock_enter_mva.assert_not_called()
        mock_ready.assert_not_called()
        page.goto.assert_not_called()

    def test_valid_vehicle_url_template_navigates_directly(self):
        """Valid templates should navigate directly before MVA entry."""
        from playwright_prototype.steps import navigate_to_mva

        page = MagicMock()
        page.url = "about:blank"
        page.goto = AsyncMock()

        wait_target = MagicMock()
        wait_target.wait_for = AsyncMock()
        filtered = MagicMock(return_value=wait_target)

        add_button = MagicMock()
        add_button.filter = filtered
        page.locator.return_value = add_button

        with patch("playwright_prototype.steps.get_config", return_value="https://example.com/vehicle/{mva}"), \
             patch("playwright_prototype.steps._open_vehicle_search_context", new=AsyncMock(return_value=page)), \
               patch("playwright_prototype.steps._enter_mva", new=AsyncMock()) as mock_enter_mva, \
                             patch("playwright_prototype.steps._wait_for_open_work_items_tab_ready", new=AsyncMock()) as mock_ready:
            asyncio.run(navigate_to_mva(page, "59000001"))

        page.goto.assert_called_once_with(
            "https://example.com/vehicle/59000001",
            wait_until="domcontentloaded",
        )
        mock_enter_mva.assert_called_once_with(page, "59000001")
        mock_ready.assert_called_once_with(page, "59000001", timeout_ms=30_000)

    def test_no_template_logs_mva_entry_path(self, caplog):
        """When no template is configured, logs should explicitly document MVA-entry mode."""
        from playwright_prototype.steps import navigate_to_mva

        page = MagicMock()
        page.url = "https://avisbudget.palantirfoundry.com/workspace/module/view/latest/ri.workshop.main.module.d62ba12c-018c-41c1-8214-0749f6591b30"
        page.goto = AsyncMock()

        wait_target = MagicMock()
        wait_target.wait_for = AsyncMock()
        add_button = MagicMock()
        add_button.filter = MagicMock(return_value=wait_target)
        page.locator.return_value = add_button

        with caplog.at_level(logging.INFO, logger="playwright_prototype.steps"):
            with patch("playwright_prototype.steps.get_config", return_value=""), \
                 patch("playwright_prototype.steps._open_vehicle_search_context", new=AsyncMock(return_value=page)), \
                 patch("playwright_prototype.steps._enter_mva", new=AsyncMock()) as mock_enter_mva, \
                 patch("playwright_prototype.steps._wait_for_open_work_items_tab_ready", new=AsyncMock()) as mock_ready:
                asyncio.run(navigate_to_mva(page, "59000001"))

        assert "no compass_vehicle_url_template configured; using MVA entry on current page" in caplog.text
        mock_enter_mva.assert_called_once_with(page, "59000001")
        mock_ready.assert_called_once_with(page, "59000001", timeout_ms=30_000)
        page.goto.assert_not_called()

    def test_ready_gate_failure_bubbles_up(self):
        """navigate_to_mva should fail when Open Work Items tab never becomes ready."""
        from playwright_prototype.steps import navigate_to_mva

        page = MagicMock()
        page.url = "about:blank"
        page.goto = AsyncMock()

        wait_target = MagicMock()
        wait_target.wait_for = AsyncMock()
        add_button = MagicMock()
        add_button.filter = MagicMock(return_value=wait_target)
        page.locator.return_value = add_button

        with patch("playwright_prototype.steps.get_config", return_value=""), \
             patch("playwright_prototype.steps._open_vehicle_search_context", new=AsyncMock(return_value=page)), \
             patch("playwright_prototype.steps._enter_mva", new=AsyncMock()), \
             patch(
                 "playwright_prototype.steps._wait_for_open_work_items_tab_ready",
                 new=AsyncMock(side_effect=RuntimeError("open work items tab not ready")),
             ):
            with pytest.raises(RuntimeError, match="open work items tab not ready"):
                asyncio.run(navigate_to_mva(page, "59000001"))


class TestVehicleValueReadiness:
    """Vehicle value readiness helpers should reject unpopulated states."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", True),
            ("   ", True),
            ("-", True),
            ("---", True),
            (" - - ", True),
            ("59042583", False),
            ("MVA: 59042583", False),
        ],
    )
    def test_is_unready_vehicle_value(self, value, expected):
        from playwright_prototype.steps import _is_unready_vehicle_value

        assert _is_unready_vehicle_value(value) is expected
