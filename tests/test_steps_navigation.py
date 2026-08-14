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


def _make_open_work_items_page(*badge_values):
    page = MagicMock()
    tabs = MagicMock()
    tab = MagicMock()
    badges = MagicMock()
    badge = MagicMock()
    tabs.count = AsyncMock(return_value=1)
    tabs.first = tab
    tab.is_visible = AsyncMock(return_value=True)
    badges.count = AsyncMock(
        side_effect=[0 if value is None else 1 for value in badge_values]
    )
    badges.first = badge
    badge.inner_text = AsyncMock(
        side_effect=[value for value in badge_values if value is not None]
    )
    tab.locator.return_value = badges
    tab.click = AsyncMock()
    page.locator.return_value = tabs
    page.wait_for_timeout = AsyncMock()
    return page, tabs, tab


class TestOpenWorkItemsZeroCountProbe:
    """The pre-click optimization must skip only on a definitive stable zero."""

    @pytest.mark.parametrize(
        "badge_text,expected",
        [
            ("0", 0),
            (" 10 ", 10),
            ("", None),
            ("zero", None),
            ("-1", None),
            ("(0)", None),
            ("0 loading", None),
        ],
    )
    def test_strict_badge_count_parser(self, badge_text, expected):
        from playwright_prototype.steps import _parse_open_work_items_badge_count

        assert _parse_open_work_items_badge_count(badge_text) == expected

    def test_two_zero_reads_confirm_without_clicking(self):
        from playwright_prototype.steps import (
            OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS,
            _has_stable_zero_open_work_items_count,
        )

        page, _, tab = _make_open_work_items_page("0", "0")

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is True
        page.wait_for_timeout.assert_awaited_once_with(OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS)
        tab.click.assert_not_awaited()

    def test_observed_production_zero_format_confirms_without_clicking(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0", "0")
        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "060333571"))

        assert result is True
        tab.click.assert_not_awaited()

    @pytest.mark.browser
    def test_exact_tab_selector_reads_nested_badge_without_clicking(self):
        from playwright.async_api import async_playwright
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        async def run_probe():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div role="tab">
                        <div>Active Complaints</div>
                        <span class="bp6-tag"><span class="bp6-fill">0</span></span>
                    </div>
                    <div role="tab" id="open-work-items">
                        <div>Open Work Items</div>
                        <span class="bp6-tag"><span class="bp6-fill count">0</span></span>
                    </div>
                    <div class="vehicle-properties-container">
                        <div class="vehicle-property__mva">
                            <div class="vehicle-property-name">MVA</div>
                            <div class="vehicle-property-value">59733310</div>
                        </div>
                    </div>
                    """
                )
                clicked = False
                await page.locator("#open-work-items").evaluate(
                    "tab => tab.addEventListener('click', () => window.tabClicked = true)"
                )

                zero_result = await _has_stable_zero_open_work_items_count(page, "059733310")
                clicked = await page.evaluate("Boolean(window.tabClicked)")
                await page.locator("#open-work-items .count").evaluate(
                    "badge => badge.textContent = '10'"
                )
                nonzero_result = await _has_stable_zero_open_work_items_count(page, "059733310")
                await browser.close()
                return zero_result, nonzero_result, clicked

        assert asyncio.run(run_probe()) == (True, False, False)

    @pytest.mark.parametrize("badge_text", ["1", "10", "unknown"])
    def test_nonzero_or_malformed_first_read_falls_through_without_delay(self, badge_text):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page(badge_text)

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        page.wait_for_timeout.assert_not_awaited()
        tab.click.assert_not_awaited()

    def test_loading_label_waits_at_most_five_seconds(self):
        from playwright_prototype.steps import (
            OPEN_WORK_ITEMS_COUNT_POLL_MS,
            OPEN_WORK_ITEMS_COUNT_WAIT_MS,
            _has_stable_zero_open_work_items_count,
        )

        max_waits = OPEN_WORK_ITEMS_COUNT_WAIT_MS // OPEN_WORK_ITEMS_COUNT_POLL_MS
        page, _, tab = _make_open_work_items_page(*([None] * (max_waits + 1)))

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        assert page.wait_for_timeout.await_count == max_waits
        assert all(
            call.args == (OPEN_WORK_ITEMS_COUNT_POLL_MS,)
            for call in page.wait_for_timeout.await_args_list
        )
        tab.click.assert_not_awaited()

    def test_loading_label_then_finalized_zero_uses_stable_confirmation(self):
        from playwright_prototype.steps import (
            OPEN_WORK_ITEMS_COUNT_POLL_MS,
            OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS,
            _has_stable_zero_open_work_items_count,
        )

        page, _, tab = _make_open_work_items_page(
            None,
            "0",
            "0",
        )
        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is True
        assert page.wait_for_timeout.await_args_list == [
            ((OPEN_WORK_ITEMS_COUNT_POLL_MS,), {}),
            ((OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS,), {}),
        ]
        tab.click.assert_not_awaited()

    def test_changing_zero_count_falls_through(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0", "1")

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        tab.click.assert_not_awaited()

    def test_missing_confirmation_preserves_first_exact_zero(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0", None)

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is True
        tab.click.assert_not_awaited()

    def test_confirmation_read_error_preserves_first_exact_zero(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0", RuntimeError("detached badge"))

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is True
        tab.click.assert_not_awaited()

    def test_hidden_confirmation_tab_preserves_first_exact_zero(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0")
        tab.is_visible.side_effect = [True, False]

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is True
        tab.click.assert_not_awaited()

    def test_duplicate_badges_fall_through_without_delay(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0")
        tab.locator.return_value.count.side_effect = [2]

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        page.wait_for_timeout.assert_not_awaited()
        tab.locator.return_value.first.inner_text.assert_not_awaited()
        tab.click.assert_not_awaited()

    @pytest.mark.parametrize("matching_tab_count", [0, 2])
    def test_missing_or_duplicate_tabs_fall_through(self, matching_tab_count):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, tabs, tab = _make_open_work_items_page("0")
        tabs.count.return_value = matching_tab_count

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        tab.locator.assert_not_called()
        tab.click.assert_not_awaited()

    def test_hidden_tab_falls_through(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page("0")
        tab.is_visible.return_value = False

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        tab.locator.assert_not_called()
        tab.click.assert_not_awaited()

    def test_read_error_falls_through(self):
        from playwright_prototype.steps import _has_stable_zero_open_work_items_count

        page, _, tab = _make_open_work_items_page(RuntimeError("detached badge"))

        result = asyncio.run(_has_stable_zero_open_work_items_count(page, "059733310"))

        assert result is False
        tab.click.assert_not_awaited()

    def test_confirmed_zero_short_circuits_before_row_lookup(self):
        from playwright_prototype.steps import close_open_work_item

        with patch(
            "playwright_prototype.steps._has_stable_zero_open_work_items_count",
            new=AsyncMock(return_value=True),
        ), patch(
            "playwright_prototype.steps._open_open_work_items_row",
            new=AsyncMock(),
        ) as open_row:
            with pytest.raises(LookupError, match="count is 0"):
                asyncio.run(close_open_work_item(MagicMock(), "059733310"))

        open_row.assert_not_awaited()

    def test_indeterminate_probe_preserves_existing_row_lookup(self):
        from playwright_prototype.steps import close_open_work_item

        with patch(
            "playwright_prototype.steps._has_stable_zero_open_work_items_count",
            new=AsyncMock(return_value=False),
        ), patch(
            "playwright_prototype.steps._open_open_work_items_row",
            new=AsyncMock(side_effect=LookupError("existing lookup")),
        ) as open_row:
            with pytest.raises(LookupError, match="existing lookup"):
                asyncio.run(close_open_work_item(MagicMock(), "059733310"))

        open_row.assert_awaited_once_with(open_row.call_args.args[0], "GLASS-GLASS")


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
                             patch("playwright_prototype.steps._wait_for_vehicle_details_ready", new=AsyncMock()) as mock_vehicle_ready, \
                             patch("playwright_prototype.steps._wait_for_open_work_items_tab_ready", new=AsyncMock()) as mock_ready:
            asyncio.run(navigate_to_mva(page, "59000001"))

        page.goto.assert_called_once_with(
            "https://example.com/vehicle/59000001",
            wait_until="domcontentloaded",
        )
        mock_enter_mva.assert_called_once_with(page, "59000001")
        mock_vehicle_ready.assert_awaited_once_with(page, "59000001", timeout_ms=10_000)
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
                  patch("playwright_prototype.steps._wait_for_vehicle_details_ready", new=AsyncMock()) as mock_vehicle_ready, \
                 patch("playwright_prototype.steps._wait_for_open_work_items_tab_ready", new=AsyncMock()) as mock_ready:
                asyncio.run(navigate_to_mva(page, "59000001"))

        assert "no compass_vehicle_url_template configured; using MVA entry on current page" in caplog.text
        mock_enter_mva.assert_called_once_with(page, "59000001")
        mock_vehicle_ready.assert_awaited_once_with(page, "59000001", timeout_ms=10_000)
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
               patch("playwright_prototype.steps._wait_for_vehicle_details_ready", new=AsyncMock()), \
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

    def test_waits_for_stale_overview_mva_to_change_to_target(self):
        from playwright_prototype.steps import _wait_for_vehicle_details_ready

        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        value_locator = MagicMock()
        value_locator.count = AsyncMock(return_value=1)
        value_locator.inner_text = AsyncMock(side_effect=["60333571", "59192781"])
        value_locator.first = value_locator
        page.locator.return_value = value_locator

        asyncio.run(_wait_for_vehicle_details_ready(page, "059192781", timeout_ms=2_000))

        assert value_locator.inner_text.await_count == 2
        value_locator.inner_text.assert_awaited_with(timeout=1_000)
        page.wait_for_timeout.assert_awaited_once_with(400)

    @pytest.mark.parametrize("overview_value", ["59192781", "059192781", "MVA: 59192781"])
    def test_accepts_exact_target_with_optional_leading_zero(self, overview_value):
        from playwright_prototype.steps import _wait_for_vehicle_details_ready

        page = MagicMock()
        value_locator = MagicMock()
        value_locator.count = AsyncMock(return_value=1)
        value_locator.inner_text = AsyncMock(return_value=overview_value)
        value_locator.first = value_locator
        page.locator.return_value = value_locator

        asyncio.run(_wait_for_vehicle_details_ready(page, "059192781", timeout_ms=1_000))

        value_locator.inner_text.assert_awaited_once_with(timeout=1_000)

    def test_rejects_nonexact_overview_mva_until_timeout(self):
        from playwright_prototype.steps import _wait_for_vehicle_details_ready

        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        value_locator = MagicMock()
        value_locator.count = AsyncMock(return_value=1)
        value_locator.inner_text = AsyncMock(return_value="1059192781")
        value_locator.first = value_locator
        page.locator.return_value = value_locator

        with pytest.raises(RuntimeError, match="vehicle details not ready"):
            asyncio.run(_wait_for_vehicle_details_ready(page, "059192781", timeout_ms=1))

    @pytest.mark.browser
    def test_reads_mva_from_supplied_overview_dom(self):
        from playwright.async_api import async_playwright
        from playwright_prototype.steps import _wait_for_vehicle_details_ready

        async def verify_overview():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div role="list">
                        <div role="listitem" class="object-set-core-sections-library__property-list-item__ws6nr8">
                            <div class="object-set-core-sections-library__property-display-name__ws6nr8">
                                <div class="bp6-text-overflow-ellipsis">MVA</div>
                            </div>
                            <div class="object-set-core-sections-library__property-display-value__ws6nr8">
                                <span class="objects-sdk-property-value-react__array-list__cfpaj6">
                                    <span class="objects-sdk-property-value-react__array-list-entry__cfpaj6">016403096</span>
                                </span>
                            </div>
                        </div>
                        <div role="listitem" class="object-set-core-sections-library__property-list-item__ws6nr8">
                            <div class="object-set-core-sections-library__property-display-name__ws6nr8">
                                <div>DW MVA No</div>
                            </div>
                            <div class="object-set-core-sections-library__property-display-value__ws6nr8">
                                <span class="objects-sdk-property-value-react__array-list-entry__cfpaj6">13611186</span>
                            </div>
                        </div>
                    </div>
                    """
                )
                await _wait_for_vehicle_details_ready(page, "016403096", timeout_ms=1_000)
                await browser.close()

        asyncio.run(verify_overview())
