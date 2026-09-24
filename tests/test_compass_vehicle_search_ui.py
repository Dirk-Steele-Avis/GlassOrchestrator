"""Browser and opt-in live tests for Compass Vehicle Search filter isolation."""
from __future__ import annotations

import os

import pytest

import create_compass_complaints as complaints

pytest.importorskip("playwright.sync_api")

STALE_MVA = "061751340"
STALE_VIN = "KNDPU3DG6T7318"
TARGET_MVA = "061279153"

VEHICLE_SEARCH_HTML = f"""
<!doctype html>
<html>
<head>
<style>
  .filter-pill {{ display: inline-flex; align-items: center; min-height: 28px; padding: 0 8px; }}
  .filter-pill svg {{ visibility: hidden; width: 16px; height: 16px; }}
  .filter-pill:hover svg {{ visibility: visible; }}
</style>
</head>
<body>
  <div id="filters">
    <span class="bp6-tag filter-pill" role="button" aria-label="Search by MVA">
      <span>Search by MVA</span><b>{STALE_MVA}</b>
      <svg data-icon="cross" role="img"></svg>
    </span>
    <span class="bp6-tag filter-pill" role="button" aria-label="Search by VIN">
      <span>Search by VIN</span><b>{STALE_VIN}</b>
      <svg data-icon="cross" role="img"></svg>
    </span>
  </div>
  <input type="search" placeholder="Keyword Search (other fields)" value="stale keyword" />
  <div data-test-id="workshop-object-table" id="results"></div>
  <div id="details"></div>
<script>
  const targetMva = "{TARGET_MVA}";
  const filters = document.getElementById("filters");
  const results = document.getElementById("results");
  const details = document.getElementById("details");

  function renderResult() {{
    const activeValues = [...document.querySelectorAll('.filter-pill b')]
      .map(element => element.textContent.trim());
    const keyword = document.querySelector('input[type="search"]').value.trim();
    const targetSelected = activeValues.includes(targetMva);
    const conflictingFilter = activeValues.some(value => value !== targetMva);
    if (!targetSelected || conflictingFilter || keyword) {{
      results.innerHTML = '';
      details.innerHTML = '';
      return;
    }}
    results.innerHTML = `<div data-test-id="workshop-object-title">${{targetMva}}</div>`;
    results.querySelector('[data-test-id="workshop-object-title"]').addEventListener('click', () => {{
      details.innerHTML = `<div role="listitem">
        <div class="property-display-name"><div>MVA</div></div>
        <div class="property-display-value"><span class="array-list-entry">${{targetMva}}</span></div>
      </div>`;
    }});
  }}

  filters.addEventListener('click', event => {{
    const pill = event.target.closest('.filter-pill');
    if (!pill) return;
    if (event.target.closest('svg[data-icon="cross"]')) {{
      pill.querySelector('b').remove();
      pill.querySelector('svg[data-icon="cross"]').remove();
      renderResult();
      return;
    }}
    if (pill.querySelector('b')) return;
    document.querySelectorAll('.bp6-popover-content').forEach(element => element.remove());
    const popover = document.createElement('div');
    popover.className = 'bp6-popover-content';
    popover.innerHTML = `<input class="bp6-multi-select-tag-input-input" placeholder="Search…" />
      <ul><li role="option"><a>${{targetMva}}</a></li></ul>`;
    document.body.appendChild(popover);
    const input = popover.querySelector('input');
    const option = popover.querySelector('li[role="option"]');
    option.style.display = 'none';
    input.addEventListener('input', () => {{
      option.style.display = input.value.trim() === targetMva ? 'list-item' : 'none';
    }});
    option.addEventListener('click', () => {{
      pill.insertAdjacentHTML('beforeend', `<b>${{targetMva}}</b><svg data-icon="cross" role="img"></svg>`);
      popover.remove();
      renderResult();
    }});
  }});
</script>
</body>
</html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(
                f"Chromium not available for Playwright: {exc}. "
                "Run '.venv\\Scripts\\python.exe -m playwright install chromium'."
            )
        context = browser.new_context()
        browser_page = context.new_page()
        yield browser_page
        context.close()
        browser.close()


@pytest.mark.browser
def test_retained_filters_hide_an_otherwise_valid_mva(page):
    page.set_content(VEHICLE_SEARCH_HTML)

    exact_result = page.locator(complaints.COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR).filter(
        has_text=TARGET_MVA
    )

    assert exact_result.count() == 0
    assert page.locator(complaints.COMPASS_ACTIVE_FILTER_SELECTOR).count() == 2
    assert page.locator(complaints.COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR).input_value() == "stale keyword"


@pytest.mark.browser
def test_search_mva_clears_history_then_selects_exact_mva(page):
    page.set_content(VEHICLE_SEARCH_HTML)

    complaints._search_mva(page, TARGET_MVA)

    assert page.locator(complaints.COMPASS_ACTIVE_FILTER_SELECTOR).count() == 1
    assert page.locator(complaints.COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR).input_value() == ""
    assert page.locator(complaints.COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR).inner_text() == TARGET_MVA
    detail_mva = page.locator(complaints.COMPASS_OVERVIEW_MVA_VALUE_SELECTOR).inner_text()
    assert detail_mva == TARGET_MVA


_RUN_LIVE_SEARCH = os.getenv("GLASS_RUN_COMPASS_SEARCH_E2E", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest.mark.e2e
@pytest.mark.skipif(
    not _RUN_LIVE_SEARCH,
    reason="Live Compass search test disabled; set GLASS_RUN_COMPASS_SEARCH_E2E=1",
)
def test_live_reused_search_clears_previous_mva_filter():
    initial_mva = os.getenv("GLASS_E2E_SEARCH_INITIAL_MVA", "").strip()
    next_mva = os.getenv("GLASS_E2E_SEARCH_NEXT_MVA", "").strip()
    if not initial_mva or not next_mva:
        pytest.skip(
            "Set GLASS_E2E_SEARCH_INITIAL_MVA and GLASS_E2E_SEARCH_NEXT_MVA "
            "to known Compass vehicles"
        )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(complaints.BROWSER_PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.new_page()
            complaints._open_home_page(page)
            vehicle_page = complaints._open_vehicle_from_home(page, initial_mva)
            complaints._search_mva(vehicle_page, next_mva)
        finally:
            context.close()
