from __future__ import annotations

import datetime
import inspect
import logging
import re
import time
from typing import TYPE_CHECKING

from config.config_loader import get_config

_GLASS_PATTERN = re.compile(r"glass|windshield|crack|chip|window", re.I)
_PM_PATTERN = re.compile(r"\bpm\b(?:\s+gas)?\b", re.I)

COMPLAINT_TYPE_PATTERNS: dict[str, re.Pattern] = {
    "Glass": _GLASS_PATTERN,
    "PM":    _PM_PATTERN,
}

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)
DATA_ENTRY_SUBMIT_DELAY_MS = 2000
BUTTON_PUSH_DELAY_MS = 2000
UI_SETTLE_DELAY_MS = 1500
OPEN_WORK_ITEMS_COUNT_WAIT_MS = 5_000
OPEN_WORK_ITEMS_COUNT_POLL_MS = 250
OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS = 250
OPEN_WORK_ITEMS_TAB_SELECTOR = (
    "xpath=//div[@role='tab'][.//div[normalize-space()='Open Work Items']]"
)
OPEN_WORK_ITEMS_BADGE_SELECTOR = "span.bp6-tag span.bp6-fill"
_OPEN_WORK_ITEMS_BADGE_TEXT_PATTERN = re.compile(r"^\d+$")

COMPASS_VEHICLES_BUTTON_SELECTOR = "[data-test-id='workshop-inline-button']"
COMPASS_SCAN_TAB_SELECTOR = 'button[role="tab"][data-key="scan"]'
COMPASS_MVA_VIN_INPUT_SELECTOR = "[data-testid='mva-vin-input']"
COMPASS_MVA_VIN_SUBMIT_SELECTOR = "[data-testid='mva-vin-submit']"
COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR = "input[type='search'][placeholder*='Keyword Search']"
COMPASS_WORKSHOP_OBJECT_TABLE_SELECTOR = "[data-test-id='workshop-object-table']"
COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR = "[data-test-id='workshop-object-title']"
COMPASS_OVERVIEW_MVA_VALUE_SELECTOR = (
    "xpath=//div[@role='listitem']"
    "[.//div[contains(@class,'property-display-name')]"
    "/div[normalize-space()='MVA']]"
    "//div[contains(@class,'property-display-value')]"
    "//span[contains(@class,'array-list-entry')]"
)


async def _has_visible_mva_input(page: Page) -> bool:
    """Return True when any known MVA input selector is visible on the current page."""
    selectors = [
        'input.bp6-input[placeholder*="Enter MVA"]',
        'input[type="text"][placeholder*="MVA"]',
        'div[role="tabpanel"][aria-hidden="false"] input[type="text"]',
        '[aria-label="Or enter MVA/VIN"]',
        COMPASS_MVA_VIN_INPUT_SELECTOR,
        COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR,
    ]
    for selector in selectors:
        try:
            field = page.locator(selector).first
            if await field.is_visible(timeout=1_500):
                return True
        except Exception:
            continue
    return False


async def _wait_for_mva_input_visible(page: Page, timeout_ms: int = 15_000) -> bool:
    """Poll for MVA input visibility until timeout and return True when found."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await _has_visible_mva_input(page):
            return True
        await page.wait_for_timeout(400)
    return False


async def _click_first_visible(locator_candidates: list) -> bool:
    """Click the first visible locator from a list and return True if a click succeeded."""
    for locator in locator_candidates:
        try:
            visible = locator.is_visible(timeout=2_000)
            if inspect.isawaitable(visible):
                visible = await visible
            if visible:
                await locator.click(timeout=8_000)
                return True
        except Exception:
            continue
    return False


async def _locator_is_visible(locator, timeout_ms: int = 2_000) -> bool:
    """Return locator visibility for both Playwright locators and mocked test locators."""
    try:
        visible = locator.is_visible(timeout=timeout_ms)
    except TypeError:
        visible = locator.is_visible()
    if inspect.isawaitable(visible):
        visible = await visible
    return bool(visible)


async def _wait_for_context_page_count(page: Page, prior_count: int, timeout_ms: int = 10_000) -> Page:
    """Wait for a new page to appear in the current context, returning it when available."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        pages = list(page.context.pages)
        if len(pages) > prior_count:
            new_page = pages[-1]
            await new_page.wait_for_load_state("domcontentloaded")
            return new_page
        await page.wait_for_timeout(250)
    return page


async def _handle_compass_go_quick_fix(page: Page, mva: str) -> None:
    """Resolve the Compass Go quick-fix interstitial when it appears."""
    quick_fix_panel = page.locator("xpath=//*[contains(normalize-space(.), 'This device needs a quick fix')]").first
    if not await _locator_is_visible(quick_fix_panel, timeout_ms=2_000):
        return

    log.warning("[STEPS] %s — Compass Go quick-fix screen detected; clicking Try again", mva)
    try_again = page.get_by_role("button", name=re.compile(r"^Try again$", re.I)).first
    await try_again.wait_for(state="visible", timeout=8_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
    await try_again.click(timeout=8_000)
    await page.wait_for_timeout(3_000)


async def _open_vehicle_search_context(page: Page, mva: str) -> Page:
    """Ensure we are on the Workshop Vehicle Search page using a strict path."""
    await _handle_compass_go_quick_fix(page, mva)

    if await _has_visible_mva_input(page):
        return page

    current_page = page
    log.info("[STEPS] %s — MVA input not visible, opening Vehicles search page", mva)

    vehicles_button = current_page.locator(COMPASS_VEHICLES_BUTTON_SELECTOR).filter(has_text="Vehicles").first
    await vehicles_button.wait_for(state="visible", timeout=8_000)

    pages_before = list(current_page.context.pages)
    await vehicles_button.click(timeout=8_000)
    await current_page.wait_for_timeout(700)
    pages_after = list(current_page.context.pages)
    if len(pages_after) > len(pages_before):
        current_page = pages_after[-1]
        await current_page.wait_for_load_state("domcontentloaded")
        log.info("[STEPS] %s — Vehicles click opened new tab: %s", mva, current_page.url)

    if await _wait_for_mva_input_visible(current_page, timeout_ms=20_000):
        log.info("[STEPS] %s — Vehicle Search context ready", mva)
        return current_page

    raise RuntimeError(
        f"[STEPS] {mva} — unable to reach Vehicle Search context (MVA input still not visible)"
    )

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _map_damage_type(location: str, action: str) -> str:
    """Map CSV location + action to the Compass glass damage type button label.

    Business rule: REPAIR is only valid for windshields; all other areas are
    always REPLACEMENT regardless of the action column value.
    """
    loc = (location or "").strip().upper()
    act = (action or "REPLACE").strip().upper()
    if loc in ("WS", "WINDSHIELD", "FRONT"):
        return "Windshield Chip" if act in ("REPAIR", "CHIP") else "Windshield Crack"
    return "Side/Rear Window Damage"


def _is_unready_vehicle_value(value: str | None) -> bool:
    """Return True when a vehicle-property value is not yet populated."""
    stripped = (value or "").strip()
    if not stripped:
        return True
    return bool(re.fullmatch(r"[-\u2010\u2011\u2012\u2013\u2014\u2015\s]+", stripped))


def _normalize_digits(value: str) -> str:
    """Return only numeric characters from the provided string."""
    return re.sub(r"\D", "", value or "")


async def _wait_for_vehicle_details_ready(page: Page, mva: str, timeout_ms: int = 20_000) -> None:
    """Wait until the vehicle details panel shows a populated MVA value for the target MVA."""
    target_digits = _normalize_digits(mva)
    accepted_values = {target_digits}
    if len(target_digits) == 9 and target_digits.startswith("0"):
        accepted_values.add(target_digits[1:])
    values = page.locator(COMPASS_OVERVIEW_MVA_VALUE_SELECTOR)

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_seen = ""
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            value_count = await values.count()
            if value_count != 1:
                last_seen = f"<MVA value count={value_count}>"
                await page.wait_for_timeout(400)
                continue

            raw_value = (await values.first.inner_text(timeout=1_000)).strip()
            last_seen = raw_value
            last_error = None
            if not _is_unready_vehicle_value(raw_value):
                digits = _normalize_digits(raw_value)
                if digits in accepted_values:
                    log.info("[STEPS] %s — vehicle details ready (MVA=%s)", mva, raw_value)
                    return
        except Exception as exc:
            last_error = exc
            log.debug("[STEPS] %s — readiness probe retry due to locator/read error: %s", mva, exc)
        await page.wait_for_timeout(400)

    extra = f"; last_error={last_error!r}" if last_error is not None else ""
    raise RuntimeError(
        f"[STEPS] {mva} — vehicle details not ready; MVA value stayed empty/hyphen or mismatched"
        f" (last_seen={last_seen!r}){extra}"
    )


async def _enter_mva(page: Page, mva: str) -> None:
    """Type an MVA into the strict Workshop search field and submit.

    No fallback selector chains are used by design.
    """
    # Prefer dedicated MVA/VIN input when present.
    mva_input = page.locator(COMPASS_MVA_VIN_INPUT_SELECTOR).first
    if await mva_input.is_visible(timeout=2_000):
        await mva_input.click(timeout=5_000)
        await mva_input.press("Control+a")
        await mva_input.press("Backspace")
        await mva_input.fill(mva)
        await page.wait_for_timeout(DATA_ENTRY_SUBMIT_DELAY_MS)

        submit_btn = page.locator(COMPASS_MVA_VIN_SUBMIT_SELECTOR).first
        await submit_btn.wait_for(state="visible", timeout=5_000)
        await submit_btn.click(timeout=5_000)
        return

    # Keyword Search mode (confirmed in user-provided screenshot).
    by_mva = page.get_by_role("button", name=re.compile(r"^Search\s+by\s+MVA$", re.I)).first
    keyword = page.locator(COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR).first

    await by_mva.wait_for(state="visible", timeout=8_000)
    await by_mva.click(timeout=5_000)

    await keyword.wait_for(state="visible", timeout=8_000)
    await keyword.click(timeout=5_000)
    await keyword.press("Control+a")
    await keyword.press("Backspace")
    await keyword.fill(mva)
    await page.wait_for_timeout(DATA_ENTRY_SUBMIT_DELAY_MS)
    await keyword.press("Enter")


async def _select_vehicle_search_result(page: Page, mva: str) -> None:
    """Select the searched vehicle from the left-side result list."""
    if await _locator_is_visible(page.get_by_role("button", name="Add Work Item").first, timeout_ms=2_000):
        return

    table = page.locator(COMPASS_WORKSHOP_OBJECT_TABLE_SELECTOR).first
    await table.wait_for(state="visible", timeout=20_000)

    title = table.locator(COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR).filter(
        has_text=re.compile(rf"^\s*{re.escape(mva)}\s*$", re.I)
    ).first

    if await title.count() == 0:
        result_row = page.locator(
            f"//div[contains(., '{mva}') and ancestor::*[contains(., 'Searched Vehicles List')]]"
        ).first
        await result_row.wait_for(state="visible", timeout=20_000)
        await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
        await result_row.click(timeout=8_000)
    else:
        await title.wait_for(state="visible", timeout=20_000)
        await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
        await title.click(timeout=8_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)


async def _wait_for_open_work_items_tab_ready(page: Page, mva: str, timeout_ms: int = 30_000) -> None:
    """Wait for the exact Open Work Items tab to appear for the selected vehicle."""
    tab = page.locator(OPEN_WORK_ITEMS_TAB_SELECTOR).first
    await tab.wait_for(state="visible", timeout=timeout_ms)
    log.info("[STEPS] %s — Open Work Items tab is visible", mva)


def _parse_open_work_items_badge_count(badge_text: str) -> int | None:
    """Parse an exact nonnegative integer from the Open Work Items badge."""
    normalized = badge_text.strip()
    return int(normalized) if _OPEN_WORK_ITEMS_BADGE_TEXT_PATTERN.fullmatch(normalized) else None


async def _read_open_work_items_badge_count(tab, mva: str) -> tuple[bool, int | None]:
    """Return whether the finalized badge exists and its strictly parsed count."""
    badges = tab.locator(OPEN_WORK_ITEMS_BADGE_SELECTOR)
    badge_count = await badges.count()
    if badge_count == 0:
        return False, None
    if badge_count != 1:
        log.warning("[STEPS] %s — expected one Open Work Items badge, found %d", mva, badge_count)
        return True, None

    raw_value = await badges.first.inner_text(timeout=1_000)
    count = _parse_open_work_items_badge_count(raw_value)
    if count is None:
        log.warning("[STEPS] %s — Open Work Items badge is unrecognized: %r", mva, raw_value.strip())
    return True, count


async def _wait_for_open_work_items_tab_count(page: Page, tab, mva: str) -> int | None:
    """Wait at most five seconds for the Open Work Items badge to appear."""
    max_waits = OPEN_WORK_ITEMS_COUNT_WAIT_MS // OPEN_WORK_ITEMS_COUNT_POLL_MS
    for attempt in range(max_waits + 1):
        badge_exists, count = await _read_open_work_items_badge_count(tab, mva)
        if count is not None:
            log.info("[STEPS] %s — Open Work Items finalized count=%d", mva, count)
            return count
        if badge_exists:
            return None
        if attempt < max_waits:
            await page.wait_for_timeout(OPEN_WORK_ITEMS_COUNT_POLL_MS)
    log.warning("[STEPS] %s — Open Work Items count did not finalize within 5s", mva)
    return None


async def _has_stable_zero_open_work_items_count(page: Page, mva: str) -> bool:
    """Return True after an exact zero unless confirmation explicitly becomes nonzero."""
    try:
        tabs = page.locator(OPEN_WORK_ITEMS_TAB_SELECTOR)
        if await tabs.count() != 1:
            return False

        tab = tabs.first
        if not await tab.is_visible(timeout=1_000):
            return False

        first_count = await _wait_for_open_work_items_tab_count(page, tab, mva)
        if first_count != 0:
            return False
    except Exception as exc:
        log.debug("[STEPS] %s — Open Work Items count probe indeterminate: %s", mva, exc)
        return False

    try:
        await page.wait_for_timeout(OPEN_WORK_ITEMS_ZERO_CONFIRM_DELAY_MS)
        if not await tab.is_visible(timeout=1_000):
            log.warning(
                "[STEPS] %s — Open Work Items badge confirmation unavailable; preserving count=0",
                mva,
            )
            return True

        badge_exists, second_count = await _read_open_work_items_badge_count(tab, mva)
        if second_count is not None and second_count > 0:
            log.info(
                "[STEPS] %s — Open Work Items count changed from 0 to %d; proceeding with lookup",
                mva,
                second_count,
            )
            return False
        if not badge_exists or second_count is None:
            log.warning(
                "[STEPS] %s — Open Work Items badge confirmation indeterminate; preserving count=0",
                mva,
            )
            return True

        log.info("[STEPS] %s — Open Work Items count=0 confirmed before tab click", mva)
        return True
    except Exception as exc:
        log.warning(
            "[STEPS] %s — Open Work Items confirmation failed after count=0; skipping lookup: %s",
            mva,
            exc,
        )
        return True


async def _open_open_work_items_row(page: Page, row_title: str) -> tuple[Page, str]:
    """Open the requested row from the Open Work Items table and return the detail page."""
    open_tab = page.locator(OPEN_WORK_ITEMS_TAB_SELECTOR).first
    await open_tab.wait_for(state="visible", timeout=15_000)
    await open_tab.scroll_into_view_if_needed(timeout=5_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
    await open_tab.click(timeout=10_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)

    title = page.locator(
        f"xpath=//div[normalize-space()='{row_title}']"
    ).first
    try:
        await title.wait_for(state="visible", timeout=15_000)
    except Exception as exc:
        raise LookupError(f"Open Work Items row not found: {row_title}") from exc
    detail_text = (await title.inner_text()).strip()

    prior_count = len(page.context.pages)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
    await title.click(timeout=10_000)
    detail_page = await _wait_for_context_page_count(page, prior_count, timeout_ms=12_000)
    if detail_page is page:
        raise RuntimeError("Open work item row click did not open a new tab")
    return detail_page, detail_text


async def _click_action_menu(page: Page) -> None:
    """Open the work-item action menu on the current details page."""
    action_menu = page.get_by_role("button", name=re.compile(r"^Action Menu$", re.I)).first
    await action_menu.wait_for(state="visible", timeout=20_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)
    await action_menu.click(timeout=10_000)
    await page.wait_for_timeout(UI_SETTLE_DELAY_MS)


async def _confirm_mark_complete(page: Page, note: str = "Done") -> None:
    """Confirm the Mark Complete dialog, filling an optional note when present."""
    dialog = page.locator("div[role='dialog'], div.bp6-dialog").first
    try:
        await dialog.wait_for(state="visible", timeout=15_000)
    except Exception as exc:
        raise RuntimeError("Mark Complete dialog not visible") from exc

    for candidate in [
        dialog.locator("textarea").first,
        dialog.locator("input[type='text']").first,
        dialog.locator("input[placeholder*='Correction']").first,
        dialog.locator("textarea[placeholder*='Correction']").first,
    ]:
        try:
            if await candidate.is_visible(timeout=500):
                await candidate.click(timeout=5_000)
                await candidate.fill(note)
                break
        except Exception:
            continue

    confirm = dialog.get_by_role("button", name=re.compile(r"^Mark Complete$", re.I)).first
    await confirm.wait_for(state="visible", timeout=10_000)
    await confirm.click(timeout=10_000)
    await dialog.wait_for(state="hidden", timeout=20_000)
    await page.wait_for_timeout(10_000)


async def close_open_work_item(page: Page, mva: str, complaint_type: str = "Glass", note: str = "Done") -> str:
    """Open the live Open Work Items row and close it through the action menu."""
    row_title = "GLASS-GLASS" if complaint_type == "Glass" else f"{complaint_type}-{complaint_type}".upper()
    log.info("[STEPS] %s — opening open-work-items row %s", mva, row_title)

    if await _has_stable_zero_open_work_items_count(page, mva):
        raise LookupError(f"Open Work Items count is 0 for MVA {mva}")

    detail_page, detail_text = await _open_open_work_items_row(page, row_title)
    log.info("[STEPS] %s — work item details opened", mva)

    await detail_page.wait_for_load_state("domcontentloaded")
    await detail_page.wait_for_timeout(UI_SETTLE_DELAY_MS)

    await _click_action_menu(detail_page)

    menu_overlay = detail_page.locator("xpath=//*[@role='menu']").first
    await menu_overlay.wait_for(state="visible", timeout=20_000)
    menu_text = (await menu_overlay.inner_text()).strip()
    log.info("[STEPS] %s — action menu opened: %s", mva, menu_text.replace("\n", " | "))

    mark_complete_menu_item = detail_page.locator(
        "xpath=(//*[@role='menu']//*[contains(normalize-space(.), 'Mark Complete')])[1]"
    ).first
    await mark_complete_menu_item.wait_for(state="visible", timeout=20_000)
    await detail_page.wait_for_timeout(UI_SETTLE_DELAY_MS)
    await mark_complete_menu_item.click(timeout=10_000)
    await detail_page.wait_for_timeout(UI_SETTLE_DELAY_MS)

    await _confirm_mark_complete(detail_page, note=note)
    log.info("[STEPS] %s — %s work item marked complete", mva, complaint_type)
    return detail_text


# ─── Warmup & Navigation ─────────────────────────────────────────────────────

async def warmup_compass(page: Page) -> None:
    """Enter a dummy MVA to fully initialize the Compass app before real MVAs.

    Mirrors mva_navigation.warmup_compass() — primes the app state so the
    first real MVA loads reliably.
    """
    dummy_mva = str(get_config("warmup_mva", "50227203"))
    log.info("[STEPS] Warming up Compass with dummy MVA %s", dummy_mva)
    try:
        await _enter_mva(page, dummy_mva)
        await page.locator("button:not([disabled])").filter(
            has_text="Add Work Item"
        ).wait_for(state="visible", timeout=30_000)
        try:
            await _wait_for_vehicle_details_ready(page, dummy_mva, timeout_ms=20_000)
        except Exception as exc:
            log.warning("[STEPS] Warm-up: vehicle details not fully confirmed (%s) — proceeding anyway", exc)
        log.info("[STEPS] Compass warm-up complete")
    except Exception:
        log.warning("[STEPS] Warm-up: 'Add Work Item' not confirmed within timeout — proceeding anyway")


async def navigate_to_mva(page: Page, mva: str) -> Page:
    """Enter an MVA and wait for the vehicle page to fully load.

    Waits for the exact 'Open Work Items' tab to be visible for the selected vehicle.
    """
    log.info("[STEPS] %s — navigating", mva)
    try:
        vehicle_url_template = str(get_config("compass_vehicle_url_template", "")).strip()
        if vehicle_url_template:
            log.info("[STEPS] %s — vehicle URL template configured: %s", mva, vehicle_url_template)
            try:
                expected_vehicle_url = vehicle_url_template.format(mva=mva)
                log.info("[STEPS] %s — resolved vehicle URL: %s", mva, expected_vehicle_url)
                if page.url != expected_vehicle_url:
                    log.info("[STEPS] %s — opening vehicle URL directly", mva)
                    await page.goto(expected_vehicle_url, wait_until="domcontentloaded")
                else:
                    log.info("[STEPS] %s — already on vehicle URL", mva)
            except Exception as exc:
                raise RuntimeError(
                    f"[STEPS] {mva} — invalid compass_vehicle_url_template: {exc}"
                ) from exc
        else:
            log.info(
                "[STEPS] %s — no compass_vehicle_url_template configured; using MVA entry on current page: %s",
                mva,
                page.url,
            )

        page = await _open_vehicle_search_context(page, mva)

        await _enter_mva(page, mva)
        await _select_vehicle_search_result(page, mva)
        await _wait_for_vehicle_details_ready(page, mva, timeout_ms=10_000)
        await _wait_for_open_work_items_tab_ready(page, mva, timeout_ms=30_000)
        log.info("[STEPS] %s — vehicle page loaded", mva)
        return page
    except Exception as exc:
        raise RuntimeError(f"[STEPS] navigate_to_mva failed for {mva}: {exc}") from exc


# ─── Work Item Flow ───────────────────────────────────────────────────────────

class ExistingWorkItemError(Exception):
    """Raised when an open work item of the requested type already exists for an MVA."""


def _parse_tile_created_at(tile_text: str) -> datetime.date | None:
    """Extract and parse the Created At date from a work item tile's inner text.

    Returns a date object or None if the field is absent or unparseable.
    Expected format: 'Created At: M/D/YYYY, H:MM:SS AM/PM'
    """
    match = re.search(r"Created At:\s*(\d{1,2}/\d{1,2}/\d{4})", tile_text, re.I)
    if not match:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), "%m/%d/%Y").date()
    except ValueError:
        return None


def _extract_complaints_text(tile_text: str) -> str:
    """Extract complaint text from a tile, returning empty string if absent."""
    for line in tile_text.splitlines():
        match = re.search(r"complaints\s*:\s*(.+)", line, re.I)
        if match:
            return match.group(1).strip()
    return ""


def _tile_matches_complaint_type(tile_text: str, pattern: re.Pattern) -> bool:
    """Match complaint type using only the complaints row to avoid timestamp false positives."""
    complaints = _extract_complaints_text(tile_text)
    return bool(complaints and pattern.search(complaints))


async def check_existing_work_item(page: Page, mva: str, complaint_type: str) -> None:
    """Raise ExistingWorkItemError when a same-type existing work item should block creation.

    Decision rules:
    - Any open same-type work item blocks creation, regardless of age.
    - Otherwise, a same-type work item created within duplicate_window_days blocks creation.
    - If Created At is missing for a same-type tile, treat as duplicate to avoid false creates.
    """
    pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, _GLASS_PATTERN)
    window_days = int(get_config("duplicate_window_days", 5))
    log.info("[STEPS] %s — checking for existing %s work item (window=%d days)", mva, complaint_type, window_days)
    try:
        container = page.locator('[class*="fleet-operations-pwa__scan-record__"]').first
        try:
            await container.wait_for(state="visible", timeout=8_000)
        except Exception:
            log.info("[STEPS] %s — no work items container found, safe to proceed", mva)
            return

        all_tiles = page.locator('[class*="fleet-operations-pwa__scan-record__"]')
        count = await all_tiles.count()
        today = datetime.date.today()
        for idx in range(count):
            tile_text = await all_tiles.nth(idx).inner_text()
            if not _tile_matches_complaint_type(tile_text, pattern):
                continue

            if re.search(r"\bopen\b", tile_text, re.I):
                raise ExistingWorkItemError(
                    f"{mva} — open {complaint_type} work item already exists: {tile_text.strip()!r}"
                )

            created_at = _parse_tile_created_at(tile_text)
            if created_at is None:
                log.warning("[STEPS] %s — %s tile has no Created At; treating as duplicate", mva, complaint_type)
                raise ExistingWorkItemError(
                    f"{mva} — {complaint_type} work item already exists (no date): {tile_text.strip()!r}"
                )

            age_days = (today - created_at).days
            if window_days == 0 or age_days <= window_days:
                raise ExistingWorkItemError(
                    f"{mva} — {complaint_type} work item already exists (created {age_days}d ago): {tile_text.strip()!r}"
                )
            log.info("[STEPS] %s — %s tile found but is %d days old (> window %d) — ignoring", mva, complaint_type, age_days, window_days)
        log.info("[STEPS] %s — no blocking %s work item found, safe to proceed", mva, complaint_type)
    except ExistingWorkItemError:
        raise
    except Exception as exc:
        raise RuntimeError(f"[STEPS] check_existing_work_item failed for {mva}: {exc}") from exc


async def click_add_work_item(page: Page, mva: str) -> None:
    """Click the 'Add Work Item' button and verify the complaint dialog opened."""
    log.info("[STEPS] %s — clicking 'Add Work Item'", mva)
    try:
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Add Work Item").click(timeout=10_000)
        # Verify complaint dialog opened — complaint list container must appear
        await page.locator(
            '[class*="fleet-operations-pwa__complaintContainer__"]'
            ', [class*="fleet-operations-pwa__complaintItem__"]'
            ', [class*="fleet-operations-pwa__addComplaint__"]'
        ).first.wait_for(state="visible", timeout=30_000)
        log.info("[STEPS] %s — 'Add Work Item' clicked — complaint dialog opened", mva)
    except Exception as exc:
        raise RuntimeError(f"[STEPS] click_add_work_item failed for {mva}: {exc}") from exc


async def _click_submit_complaint(page: Page, mva: str) -> None:
    """Click Submit Complaint; raises RuntimeError if all click strategies fail."""
    submit_button = page.get_by_role(
        "button", name=re.compile(r"Submit Complaint|Submit", re.I)
    ).first
    await submit_button.wait_for(state="visible", timeout=20_000)

    last_exc: Exception | None = None

    await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
    try:
        await submit_button.click(timeout=8_000)
        return
    except Exception as exc:
        last_exc = exc
        log.warning("[STEPS] %s — submit click failed, retrying with force=True: %s", mva, exc)

    await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
    try:
        await submit_button.click(timeout=8_000, force=True)
        return
    except Exception as exc:
        last_exc = exc
        log.warning("[STEPS] %s — force click failed, retrying via JS evaluate: %s", mva, exc)

    handle = await submit_button.element_handle()
    if handle is None:
        raise RuntimeError(f"[STEPS] {mva} — submit button handle unavailable after 2 failed clicks") from last_exc
    await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
    try:
        await page.evaluate("(el) => el.click()", handle)
    except Exception as exc:
        raise RuntimeError(f"[STEPS] {mva} — all 3 submit click strategies failed") from exc


async def _wait_for_post_submit_progress(page: Page, previous_url: str) -> bool:
    """Return True when submit progresses to a new state (URL change or mileage UI)."""
    try:
        await page.wait_for_function(
            "prev => window.location.href !== prev",
            arg=previous_url,
            timeout=10_000,
        )
        return True
    except Exception:
        pass

    mileage_locators = [
        page.get_by_role("heading", name=re.compile(r"Mileage", re.I)),
        page.get_by_text(re.compile(r"\bMileage\b", re.I)),
        page.locator('input[placeholder*="Mileage" i], input[aria-label*="Mileage" i]'),
    ]
    for locator in mileage_locators:
        try:
            await locator.first.wait_for(state="visible", timeout=4_000)
            return True
        except Exception:
            continue
    return False


async def handle_complaint_dialog(page: Page, mva: str, complaint_type: str, location: str, action: str, step_delay_ms: int = 0) -> None:
    """Associate an existing complaint or create a new one, branching by type (Glass or PM).

    Existing path: find matching complaint tile → click → Next (advances to mileage).
    New path: Add New Complaint → Drivability → type-specific buttons → Submit Complaint.
    Both paths leave the page on the mileage dialog for complete_mileage_dialog().
    """
    log.info("[STEPS] %s — handling complaint dialog (type=%s location=%s action=%s)", mva, complaint_type, location, action)

    async def delay():
        if step_delay_ms:
            await page.wait_for_timeout(step_delay_ms)

    try:
        await page.wait_for_timeout(2_000)

        pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, _GLASS_PATTERN)
        existing_tile = page.locator(
            '[class*="fleet-operations-pwa__complaintItem__"]'
        ).filter(has_text=pattern)

        if await existing_tile.count() > 0:
            log.info("[STEPS] %s — found existing %s complaint, associating", mva, complaint_type)
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await existing_tile.first.click(timeout=5_000);  await delay()
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await page.get_by_role("button", name="Next").click(timeout=10_000)
            mileage_appeared = False
            for locator in [
                page.get_by_role("heading", name=re.compile(r"Mileage", re.I)),
                page.get_by_text(re.compile(r"\bMileage\b", re.I)),
                page.locator('input[placeholder*="Mileage" i], input[aria-label*="Mileage" i]'),
            ]:
                try:
                    await locator.first.wait_for(state="visible", timeout=8_000)
                    mileage_appeared = True
                    break
                except Exception:
                    continue
            if not mileage_appeared:
                raise RuntimeError(f"[STEPS] {mva} — existing complaint Next did not advance to mileage dialog")
            return

        # No existing complaint — create new
        log.info("[STEPS] %s — no existing %s complaint, creating new", mva, complaint_type)
        add_btn = page.locator(
            "//button[.//p[contains(text(),'Add New Complaint')] or .//p[contains(text(),'Create New Complaint')]]"
            " | //button[normalize-space()='Add New Complaint']"
            " | //button[normalize-space()='Create New Complaint']"
        ).first
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await add_btn.click(timeout=10_000);  await delay()

        drivability = str(get_config("default_drivability", "Yes"))
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name=drivability).click(timeout=10_000)
        log.info("[STEPS] %s — drivability: %s", mva, drivability);  await delay()

        if complaint_type == "PM":
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await page.get_by_role("button", name="PM").click(timeout=10_000)
            log.info("[STEPS] %s PM: PM button clicked", mva);  await delay()

            # Wait for Additional Info screen — leave checkbox at default (unchecked), skip photo
            await page.locator('[class*="fleet-operations-pwa__"]').filter(
                has_text=re.compile(r"additional info", re.I)
            ).first.wait_for(state="visible", timeout=15_000)
            log.info("[STEPS] %s PM: Additional Info screen visible", mva);  await delay()

            pre_submit_url = page.url
            await _click_submit_complaint(page, mva)
            log.info("[STEPS] %s PM: PM complaint submitted", mva)

            if not await _wait_for_post_submit_progress(page, pre_submit_url):
                pm_tile_post = page.locator(
                    '[class*="fleet-operations-pwa__complaintItem__"]'
                ).filter(has_text=_PM_PATTERN)
                if await pm_tile_post.count() > 0:
                    log.info("[STEPS] %s PM: post-submit complaint list shown, associating", mva)
                    await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
                    await pm_tile_post.first.click(timeout=5_000);  await delay()
                    await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
                    await page.get_by_role("button", name="Next").click(timeout=10_000)

                if not await _wait_for_post_submit_progress(page, pre_submit_url):
                    raise RuntimeError(
                        f"[STEPS] {mva} PM — submit completed without mileage/url transition"
                    )
            return

        # Glass path
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Glass Damage").click(timeout=10_000);  await delay()

        damage_label = _map_damage_type(location, action)
        log.info("[STEPS] %s — selecting damage type: %s", mva, damage_label)
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.locator(f'//button[.//h1[text()="{damage_label}"]]').click(timeout=10_000);  await delay()

        pre_submit_url = page.url
        await _click_submit_complaint(page, mva)
        log.info("[STEPS] %s — new glass complaint submitted", mva)

        if await _wait_for_post_submit_progress(page, pre_submit_url):
            return

        log.warning(
            "[STEPS] %s — submit did not show mileage/url transition; attempting complaint association fallback",
            mva,
        )
        await page.wait_for_timeout(2_000)
        glass_tile_post = page.locator(
            '[class*="fleet-operations-pwa__complaintItem__"]'
        ).filter(has_text=_GLASS_PATTERN)
        if await glass_tile_post.count() > 0:
            log.info("[STEPS] %s — post-submit: complaint list shown, associating new complaint", mva)
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await glass_tile_post.first.click(timeout=5_000);  await delay()
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await page.get_by_role("button", name="Next").click(timeout=10_000)

        if not await _wait_for_post_submit_progress(page, pre_submit_url):
            raise RuntimeError(
                f"[STEPS] {mva} — submit completed without mileage/url transition; backend may have rejected write"
            )

    except Exception as exc:
        raise RuntimeError(f"[STEPS] handle_complaint_dialog failed for {mva}: {exc}") from exc


async def complete_mileage_dialog(page: Page, mva: str) -> None:
    """Advance past the mileage dialog by clicking Next.

    Mirrors mileage_flows.complete_mileage_dialog() — the mileage value is
    typically pre-populated from the vehicle record.
    Verifies the OpCode list appears after Next is clicked.
    """
    log.info("[STEPS] %s — advancing past mileage dialog", mva)
    try:
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Next").click(timeout=10_000)
        # Verify mileage dialog dismissed — OpCode list must appear
        await page.locator('[class*="opCodeText"]').first.wait_for(state="visible", timeout=15_000)
        log.info("[STEPS] %s — mileage dialog advanced", mva)
    except Exception as exc:
        raise RuntimeError(f"[STEPS] complete_mileage_dialog failed for {mva}: {exc}") from exc


async def select_opcode(page: Page, complaint_type: str) -> None:
    """Select the appropriate opcode for the given work item type.

    Glass: selects glass_opcode_primary (default 'Glass Repair/Replace').
    PM: selects pm_opcode config value (default 'PM Gas'); skips step if pm_opcode is null.
    """
    if complaint_type == "PM":
        pm_opcode = get_config("pm_opcode", None)
        if pm_opcode is None:
            log.info("[STEPS] PM: pm_opcode is null — skipping opcode selection")
            return
        opcode_label = str(pm_opcode)
    else:
        opcode_label = str(get_config("glass_opcode_primary", "Glass Repair/Replace"))

    log.info("[STEPS] Selecting '%s' OpCode", opcode_label)
    try:
        await page.locator('[class*="opCodeText"]').first.wait_for(
            state="visible", timeout=15_000
        )
        target = page.get_by_text(opcode_label, exact=True)
        await target.scroll_into_view_if_needed()
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await target.click(timeout=10_000)
        await page.get_by_role("button", name="Create Work Item").wait_for(
            state="visible", timeout=15_000
        )
        log.info("[STEPS] OpCode '%s' selected — 'Create Work Item' button visible", opcode_label)
    except Exception as exc:
        raise RuntimeError(f"[STEPS] select_opcode failed for complaint_type={complaint_type}: {exc}") from exc


async def create_work_item(page: Page) -> None:
    """Click the 'Create Work Item' button.

    Tries an exact text match first (matches finalize_flow.py XPath). Falls
    back to the enabled-button-in-container heuristic if the button has no
    visible label (as seen in earlier Compass versions).
    """
    log.info("[STEPS] Clicking 'Create Work Item' button")
    try:
        button = page.get_by_role("button", name="Create Work Item")
        if await button.count() == 0:
            log.info("[STEPS] Exact name not found — using container fallback")
            button = page.locator(
                "[class*='fleet-operations-pwa__generalContainer__'] "
                "button:not([class*='bp6-disabled'])"
            ).first
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await button.click(timeout=10_000)
        log.info("[STEPS] 'Create Work Item' clicked")
        # Verify server accepted — Done button must appear on completion dialog
        await page.get_by_role("button", name="Done").wait_for(state="visible", timeout=30_000)
        log.info("[STEPS] 'Create Work Item' confirmed — Done button visible")
    except Exception as exc:
        raise RuntimeError(f"[STEPS] create_work_item failed: {exc}") from exc


async def confirm_completion(page: Page) -> None:
    """Click the final 'Done' button on the completion dialog.

    Verifies the work items list reappears with at least one open item,
    confirming the work item was persisted.
    """
    log.info("[STEPS] Clicking 'Done' button")
    try:
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Done").click(timeout=10_000)
        # Verify work item persisted — work items container must reappear
        await page.locator(
            "div[class*='fleet-operations-pwa__scan-record__']"
        ).first.wait_for(state="visible", timeout=20_000)
        log.info("[STEPS] 'Done' clicked — work item confirmed in list")
    except Exception as exc:
        raise RuntimeError(f"[STEPS] confirm_completion failed: {exc}") from exc


# ─── Close / Resolve Work Item ────────────────────────────────────────────────

async def open_work_item_tile(page: Page, mva: str, complaint_type: str = "Glass") -> None:
    """Click the matching open work item tile and verify details are shown."""
    log.info("[STEPS] %s — opening %s work item tile", mva, complaint_type)
    pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, _GLASS_PATTERN)
    try:
        open_tiles = page.locator(
            "div[class*='fleet-operations-pwa__scan-record__']"
        ).filter(
            has_text=re.compile(r"open", re.I)
        )

        count = await open_tiles.count()
        if count == 0:
            raise RuntimeError(f"[STEPS] {mva} — no open work item tiles found")

        for idx in range(count):
            tile = open_tiles.nth(idx)
            tile_text = (await tile.inner_text()).strip()
            if not _tile_matches_complaint_type(tile_text, pattern):
                continue

            await tile.wait_for(state="visible", timeout=10_000)
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await tile.locator("text=Open").first.click(timeout=8_000)
            await page.get_by_role("button", name="Mark Complete").wait_for(
                state="visible", timeout=15_000
            )
            log.info("[STEPS] %s — %s work item tile opened", mva, complaint_type)
            return

        raise RuntimeError(f"[STEPS] {mva} — no open {complaint_type} tile matched complaints row")
    except Exception as exc:
        raise RuntimeError(f"[STEPS] open_work_item_tile failed for {mva}: {exc}") from exc


async def open_glass_work_item_tile(page: Page, mva: str, complaint_type: str = "Glass", **kwargs) -> None:
    """Backward-compatible wrapper for open_work_item_tile()."""
    legacy_type = kwargs.get("type")
    await open_work_item_tile(page, mva, complaint_type=legacy_type or complaint_type)


async def complete_work_item(page: Page, mva: str, note: str = "Done", complaint_type: str = "Glass") -> None:
    """Click 'Mark Complete', fill correction, and complete the selected work item."""
    log.info("[STEPS] %s — marking %s work item complete", mva, complaint_type)
    pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, _GLASS_PATTERN)
    try:
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Mark Complete").click(timeout=10_000)

        correction = page.locator(
            'textarea[class*="textAreaContainer"], '
            'textarea[placeholder*="Enter Correction"], input[placeholder*="Enter Correction"]'
        ).first
        await correction.wait_for(state="visible", timeout=15_000)
        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await correction.click(timeout=5_000)
        await correction.fill(note)

        await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
        await page.get_by_role("button", name="Complete Work Item").click(timeout=10_000)

        await page.locator(
            "div[class*='fleet-operations-pwa__scan-record__']"
        ).filter(
            has_text=pattern
        ).filter(
            has_text=re.compile(r"complete", re.I)
        ).first.wait_for(state="visible", timeout=20_000)

        log.info("[STEPS] %s — %s work item marked complete", mva, complaint_type)
    except Exception as exc:
        raise RuntimeError(f"[STEPS] complete_work_item failed for {mva}: {exc}") from exc


async def complete_glass_work_item(page: Page, mva: str, note: str = "Done", complaint_type: str = "Glass", **kwargs) -> None:
    """Backward-compatible wrapper for complete_work_item()."""
    legacy_type = kwargs.get("type")
    await complete_work_item(page, mva, note=note, complaint_type=legacy_type or complaint_type)
