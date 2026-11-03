"""
Unit tests for navigate_to_mva() fail-fast behavior in playwright_prototype/steps.py.

Focus:
- invalid compass_vehicle_url_template must fail immediately with diagnostics
- valid compass_vehicle_url_template performs direct navigation
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


def test_compass_home_vehicle_search_submits_exact_mva():
    from playwright_prototype.steps import (
        COMPASS_HOME_MVA_INPUT_SELECTOR,
        DATA_ENTRY_SUBMIT_DELAY_MS,
        _enter_mva,
    )

    page = MagicMock()
    mva_input = MagicMock()
    mva_input.count = AsyncMock(return_value=1)
    mva_input.first = mva_input
    mva_input.is_visible = AsyncMock(return_value=True)
    mva_input.fill = AsyncMock()
    search_button = MagicMock()
    search_button.count = AsyncMock(return_value=1)
    search_button.first = search_button
    search_button.click = AsyncMock()
    vehicle_page = MagicMock()
    vehicle_page.wait_for_load_state = AsyncMock()
    new_page_info = MagicMock()
    new_page_info.value = AsyncMock(return_value=vehicle_page)()
    expect_page = AsyncMock()
    expect_page.__aenter__ = AsyncMock(return_value=new_page_info)
    expect_page.__aexit__ = AsyncMock(return_value=None)
    page.context.expect_page.return_value = expect_page
    page.locator.return_value = mva_input
    page.get_by_role.return_value = search_button
    page.wait_for_timeout = AsyncMock()

    result = asyncio.run(_enter_mva(page, "059733310"))

    assert result is vehicle_page
    page.locator.assert_called_once_with(COMPASS_HOME_MVA_INPUT_SELECTOR)
    page.get_by_role.assert_called_once_with("button", name="Search", exact=True)
    mva_input.fill.assert_awaited_once_with("059733310")
    page.wait_for_timeout.assert_awaited_once_with(DATA_ENTRY_SUBMIT_DELAY_MS)
    page.context.expect_page.assert_called_once_with(timeout=30_000)
    search_button.click.assert_awaited_once_with(timeout=30_000)
    vehicle_page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")


def test_compass_home_vehicle_search_click_timeout_fails_without_retry():
    from playwright_prototype.steps import navigate_to_mva

    page = MagicMock()
    page.url = "https://compass.example/home"
    mva_input = MagicMock()
    mva_input.count = AsyncMock(return_value=1)
    mva_input.first = mva_input
    mva_input.is_visible = AsyncMock(return_value=True)
    mva_input.fill = AsyncMock()
    search_button = MagicMock()
    search_button.count = AsyncMock(return_value=1)
    search_button.first = search_button
    search_button.click = AsyncMock(side_effect=TimeoutError("Locator.click: Timeout 30000ms exceeded."))
    expect_page = AsyncMock()
    expect_page.__aenter__ = AsyncMock(return_value=MagicMock())
    expect_page.__aexit__ = AsyncMock(return_value=None)
    page.context.expect_page.return_value = expect_page
    page.locator.return_value = mva_input
    page.get_by_role.return_value = search_button
    page.wait_for_timeout = AsyncMock()

    with patch("playwright_prototype.steps.get_config", return_value=""), patch(
        "playwright_prototype.steps._open_vehicle_search_context", AsyncMock(return_value=page)
    ), patch("playwright_prototype.steps._search_by_mva_filter", AsyncMock()) as filter_search:
        with pytest.raises(RuntimeError, match=r"navigate_to_mva failed for 062296533: .*Timeout 30000ms"):
            asyncio.run(navigate_to_mva(page, "062296533"))

    search_button.click.assert_awaited_once_with(timeout=30_000)
    filter_search.assert_not_awaited()


def test_compass_home_vehicle_search_rejects_ambiguous_search_button():
    from playwright_prototype.steps import _enter_mva

    page = MagicMock()
    mva_input = MagicMock()
    mva_input.count = AsyncMock(return_value=1)
    mva_input.first = mva_input
    mva_input.is_visible = AsyncMock(return_value=True)
    search_button = MagicMock()
    search_button.count = AsyncMock(return_value=2)
    page.locator.return_value = mva_input
    page.get_by_role.return_value = search_button

    with pytest.raises(RuntimeError, match="exactly one Search button"):
        asyncio.run(_enter_mva(page, "059733310"))

    mva_input.fill.assert_not_called()


def test_clear_vehicle_search_filters_removes_every_filter_and_keyword():
    from playwright_prototype.steps import (
        COMPASS_ACTIVE_FILTER_SELECTOR,
        COMPASS_FILTER_CLEAR_SELECTOR,
        COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR,
        _clear_vehicle_search_filters,
    )

    page = MagicMock()
    active_filters = MagicMock()
    active_filter = MagicMock()
    clear = MagicMock()
    keyword = MagicMock()

    active_filters.count = AsyncMock(side_effect=[2, 1, 0])
    active_filters.first = active_filter
    active_filter.inner_text = AsyncMock(
        side_effect=["Search by MVA 011111111", "Search by VIN TESTVIN"]
    )
    active_filter.hover = AsyncMock()
    active_filter.locator.return_value = clear
    clear.count = AsyncMock(return_value=1)
    clear.first = clear
    clear.is_visible = AsyncMock(return_value=True)
    clear.click = AsyncMock()
    keyword.count = AsyncMock(return_value=1)
    keyword.first = keyword
    keyword.wait_for = AsyncMock()
    keyword.fill = AsyncMock()
    page.locator.side_effect = lambda selector: {
        COMPASS_ACTIVE_FILTER_SELECTOR: active_filters,
        COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR: keyword,
    }[selector]
    page.wait_for_timeout = AsyncMock()

    asyncio.run(_clear_vehicle_search_filters(page))

    assert active_filter.hover.await_count == 2
    assert active_filter.locator.call_args_list == [
        call(COMPASS_FILTER_CLEAR_SELECTOR),
        call(COMPASS_FILTER_CLEAR_SELECTOR),
    ]
    assert clear.click.await_count == 2
    keyword.wait_for.assert_awaited_once_with(state="visible", timeout=8_000)
    keyword.fill.assert_awaited_once_with("")


def test_reused_vehicle_search_selects_exact_mva_filter_option():
    from playwright_prototype.steps import (
        COMPASS_HOME_MVA_INPUT_SELECTOR,
        COMPASS_MVA_FILTER_INPUT_SELECTOR,
        COMPASS_MVA_FILTER_OPTION_SELECTOR,
        _enter_mva,
    )

    page = MagicMock()
    home_input = MagicMock()
    home_input.count = AsyncMock(return_value=0)
    filter_input = MagicMock()
    filter_input.first = filter_input
    filter_input.wait_for = AsyncMock()
    filter_input.count = AsyncMock(return_value=1)
    filter_input.fill = AsyncMock()
    options = MagicMock()
    exact_options = MagicMock()
    options.filter.return_value = exact_options
    exact_options.first = exact_options
    exact_options.wait_for = AsyncMock()
    exact_options.count = AsyncMock(return_value=1)
    exact_options.click = AsyncMock()
    by_mva = MagicMock()
    by_mva.count = AsyncMock(return_value=1)
    by_mva.first = by_mva
    by_mva.wait_for = AsyncMock()
    by_mva.click = AsyncMock()
    page.locator.side_effect = lambda selector: {
        COMPASS_HOME_MVA_INPUT_SELECTOR: home_input,
        COMPASS_MVA_FILTER_INPUT_SELECTOR: filter_input,
        COMPASS_MVA_FILTER_OPTION_SELECTOR: options,
    }[selector]
    page.get_by_role.return_value = by_mva

    with patch(
        "playwright_prototype.steps._clear_vehicle_search_filters",
        new=AsyncMock(),
    ) as clear_filters:
        result = asyncio.run(_enter_mva(page, "059733310"))

    assert result is page
    clear_filters.assert_awaited_once_with(page)
    by_mva.wait_for.assert_awaited_once_with(state="visible", timeout=8_000)
    by_mva.click.assert_awaited_once_with(timeout=5_000)
    assert filter_input.fill.await_args_list == [call(""), call("059733310")]
    exact_options.click.assert_awaited_once_with(timeout=5_000)


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
            with pytest.raises(LookupError, match="No work item found"):
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

    @staticmethod
    def _detail_page() -> tuple[MagicMock, MagicMock]:
        detail_page = MagicMock()
        detail_page.wait_for_load_state = AsyncMock()
        detail_page.wait_for_timeout = AsyncMock()
        menu = MagicMock()
        menu.wait_for = AsyncMock()
        menu.inner_text = AsyncMock(return_value="Mark Complete")
        item = MagicMock()
        item.click = AsyncMock()
        item.all_inner_texts = AsyncMock(return_value=["Mark Complete\nWork item must be open"])
        menu.get_by_role.return_value = item
        detail_page.locator.return_value.first = menu
        detail_page.close = AsyncMock()
        return detail_page, item

    def test_success_waits_for_enabled_then_settles_before_click(self):
        from playwright_prototype.steps import (
            MARK_COMPLETE_ENABLED_TIMEOUT_MS,
            MARK_COMPLETE_MENU_ITEM_NAME,
            MARK_COMPLETE_POST_ENABLED_SETTLE_MS,
            close_open_work_item,
        )

        vehicle_page = MagicMock()
        vehicle_page.bring_to_front = AsyncMock()
        detail_page, item = self._detail_page()
        events = []
        detail_page.wait_for_timeout.side_effect = lambda ms: events.append(("wait", ms))
        item.click.side_effect = lambda **kwargs: events.append(("click", kwargs))

        async def wait_enabled(locator, timeout_ms):
            events.append(("enabled", locator, timeout_ms))

        with patch(
            "playwright_prototype.steps._has_stable_zero_open_work_items_count",
            new=AsyncMock(return_value=False),
        ), patch(
            "playwright_prototype.steps._open_open_work_items_row",
            new=AsyncMock(return_value=(detail_page, "GLASS-GLASS")),
        ), patch(
            "playwright_prototype.steps._click_action_menu",
            new=AsyncMock(),
        ), patch(
            "playwright_prototype.steps._wait_until_enabled",
            new=AsyncMock(side_effect=wait_enabled),
        ), patch(
            "playwright_prototype.steps._confirm_mark_complete",
            new=AsyncMock(),
        ):
            result = asyncio.run(close_open_work_item(vehicle_page, "050106803"))

        assert result == "GLASS-GLASS"
        assert detail_page.locator.return_value.first.get_by_role.call_args_list[0] == call(
            "menuitem", name=MARK_COMPLETE_MENU_ITEM_NAME
        )
        assert MARK_COMPLETE_POST_ENABLED_SETTLE_MS == 1_000
        start = next(i for i, event in enumerate(events) if event[0] == "enabled")
        assert events[start:start + 3] == [
            ("enabled", item, MARK_COMPLETE_ENABLED_TIMEOUT_MS),
            ("wait", MARK_COMPLETE_POST_ENABLED_SETTLE_MS),
            ("click", {"timeout": 10_000}),
        ]
        detail_page.close.assert_awaited_once_with()
        vehicle_page.bring_to_front.assert_awaited_once_with()

    def test_mark_complete_never_enabled_fails_without_click(self):
        from playwright_prototype.steps import close_open_work_item

        detail_page, item = self._detail_page()

        with patch(
            "playwright_prototype.steps._has_stable_zero_open_work_items_count",
            new=AsyncMock(return_value=False),
        ), patch(
            "playwright_prototype.steps._open_open_work_items_row",
            new=AsyncMock(return_value=(detail_page, "GLASS-GLASS")),
        ), patch(
            "playwright_prototype.steps._click_action_menu",
            new=AsyncMock(),
        ), patch(
            "playwright_prototype.steps._wait_until_enabled",
            new=AsyncMock(side_effect=RuntimeError("Mark Complete not enabled within 20000 ms")),
        ), patch(
            "playwright_prototype.steps._confirm_mark_complete",
            new=AsyncMock(),
        ) as confirm:
            with pytest.raises(RuntimeError, match="not enabled within 20000 ms"):
                asyncio.run(close_open_work_item(MagicMock(), "062296533"))

        item.click.assert_not_awaited()
        confirm.assert_not_awaited()
        detail_page.close.assert_not_awaited()

    @pytest.mark.browser
    def test_wait_until_enabled_tracks_delayed_menu_item_enable(self):
        from playwright.async_api import async_playwright
        from playwright_prototype.steps import MARK_COMPLETE_MENU_ITEM_NAME, _wait_until_enabled

        menu_html = """
            <ul role="menu" class="bp6-menu">
              <li role="none"><a id="mc" role="menuitem" data-test-id="workshop-menu-item-button"
                 tabindex="-1" aria-disabled="true" class="bp6-menu-item bp6-disabled">
                 <div class="bp6-fill"><div>Mark Complete</div>
                 <div class="bp6-text-small">Work item must be open and have an op code</div></div></a></li>
              <li role="none"><a role="menuitem" data-test-id="workshop-menu-item-button"
                 class="bp6-menu-item"><div class="bp6-fill"><div>Mark Canceled</div></div></a></li>
            </ul>
        """

        async def run_probe():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(
                    menu_html
                    + "<script>setTimeout(() => document.getElementById('mc')"
                    ".removeAttribute('aria-disabled'), 700)</script>"
                )
                item = page.get_by_role("menu").get_by_role("menuitem", name=MARK_COMPLETE_MENU_ITEM_NAME)
                assert await item.is_disabled()
                await _wait_until_enabled(item, 5_000)
                enabled = await item.is_enabled()

                await page.set_content(menu_html)
                stuck = page.get_by_role("menu").get_by_role("menuitem", name=MARK_COMPLETE_MENU_ITEM_NAME)
                with pytest.raises(RuntimeError, match="not enabled within 500 ms"):
                    await _wait_until_enabled(stuck, 500)
                await browser.close()
                return enabled

        assert asyncio.run(run_probe()) is True

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
               patch("playwright_prototype.steps._enter_mva", new=AsyncMock(return_value=page)) as mock_enter_mva, \
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
                             patch("playwright_prototype.steps._enter_mva", new=AsyncMock(return_value=page)) as mock_enter_mva, \
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
                 patch("playwright_prototype.steps._enter_mva", new=AsyncMock(return_value=page)) as mock_enter_mva, \
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
             patch("playwright_prototype.steps._enter_mva", new=AsyncMock(return_value=page)), \
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
