from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

from playwright_prototype.config import FOUNDRY_HOME_URL, LOGIN_URL, SSO_EMAIL, STORAGE_STATE_PATH
from playwright_prototype.login import (
    click_compass_mobile_tile,
    enter_wwid,
    perform_full_login,
    select_sso_account,
)

log = logging.getLogger(__name__)
BUTTON_PUSH_DELAY_MS = 2000
WORKSHOP_VEHICLES_BUTTON_SELECTOR = "[data-test-id='workshop-inline-button']"


def _credentials() -> tuple[str, str, str, str]:
    """Read Compass login credentials — env vars take priority, config file as fallback."""
    from config.config_loader import get_config
    username = os.getenv("GLASS_LOGIN_USERNAME") or get_config("username")
    password = os.getenv("GLASS_LOGIN_PASSWORD") or get_config("password")
    login_id = os.getenv("GLASS_LOGIN_ID") or get_config("login_id")
    return username, password, login_id, SSO_EMAIL


async def _is_on_login_page(page: Page) -> bool:
    """True if the Microsoft SSO email form is visible (session expired or fresh start)."""
    try:
        await page.locator('input[name="loginfmt"]').wait_for(state="visible", timeout=3_000)
        return True
    except Exception:
        return False


async def _is_on_sso_picker(page: Page) -> bool:
    """True if the 'Pick an account' Microsoft SSO dialog is showing."""
    try:
        await page.locator(
            '[aria-label*="Pick an account"], [data-testid="sso-page-identifier"]'
        ).wait_for(state="visible", timeout=3_000)
        return True
    except Exception:
        return False


async def _select_first_sso_account(page: Page) -> None:
    """Select the first account tile on the Microsoft SSO picker."""
    selectors = [
        '[data-testid="account-tile"]',
        '#tilesHolder div[role="button"]',
        '#tilesHolder .table',
        '[role="listitem"]',
        'div[data-test-id="userTile"]',
        'div.table[role="button"]',
    ]
    for selector in selectors:
        try:
            account_tile = page.locator(selector).first
            await account_tile.wait_for(state="visible", timeout=3_000)
            await page.wait_for_timeout(BUTTON_PUSH_DELAY_MS)
            await account_tile.click(timeout=8_000)
            log.info("[SESSION] Selected first SSO account tile via selector: %s", selector)
            return
        except Exception:
            continue

    raise RuntimeError("[SESSION] Unable to find/click first SSO account tile on picker")


def _score_attached_page(page: Page) -> int:
    """Score candidate pages so attach mode picks the most relevant tab."""
    url = (page.url or "").lower()
    if "login.microsoftonline.com" in url:
        return 100
    if "palantirfoundry.com" in url:
        return 80
    if "fleet-operations" in url or "compass" in url:
        return 70
    return 10


async def _is_on_compass_mobile_picker(page: Page) -> bool:
    """True if the Foundry workspace app tile for Compass Mobile is visible."""
    from config.config_loader import get_config
    try:
        label = str(get_config("compass_app_label", "Compass Mobile"))
        await page.locator(
            f"//a[@role='button']//span[contains(normalize-space(.), '{label}')]"
            f" | //a[@role='button'][.//*[contains(normalize-space(.), '{label}')]]"
            f" | //button[.//*[contains(normalize-space(.), '{label}')]]"
            f" | //*[contains(normalize-space(.), '{label}')]"
        ).first.wait_for(state="visible", timeout=12_000)
        return True
    except Exception:
        return False


async def _is_on_wwid_screen(page: Page) -> bool:
    """True if the Compass Mobile WWID entry input is visible.

    Uses the Submit button as the discriminator — the app page never has a
    'Submit' button, but the WWID entry screen always does.
    """
    try:
        await page.get_by_role("button", name="Submit").wait_for(state="visible", timeout=3_000)
        return True
    except Exception:
        return False


async def _is_on_compass_app_page(page: Page) -> bool:
    """True when already inside Compass app after cached auth/session restore."""
    candidates = [
        'input.bp6-input[placeholder*="Enter MVA"]',
        'input[type="text"][placeholder*="MVA"]',
        'button:has-text("Add Work Item")',
    ]
    for selector in candidates:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=3_000)
            return True
        except Exception:
            continue
    return False


async def _is_on_workshop_home(page: Page) -> bool:
    """True when the configured Workshop homepage is ready for vehicle navigation."""
    if not (page.url or "").lower().startswith(LOGIN_URL.lower()):
        return False
    try:
        vehicles_button = page.locator(WORKSHOP_VEHICLES_BUTTON_SELECTOR).filter(has_text="Vehicles").first
        await vehicles_button.wait_for(state="visible", timeout=8_000)
        return True
    except Exception:
        return False


async def _advance_existing_session_page(page: Page) -> Page:
    """Advance an existing trusted session page into the Compass app."""
    current_url = (page.url or "").lower()
    if not current_url or current_url == "about:blank":
        log.info("[SESSION] Existing session page blank — opening Foundry home URL")
        await page.goto(FOUNDRY_HOME_URL, wait_until="domcontentloaded")
        current_url = (page.url or "").lower()

    if current_url and not current_url.startswith(LOGIN_URL.lower()):
        log.info("[SESSION] Existing session page not on login URL — opening login URL: %s", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await _is_on_workshop_home(page):
        log.info("[SESSION] Existing session page on Workshop Home — ready for vehicle search")
        return page

    if await _is_on_login_page(page):
        log.info("[SESSION] Existing session page is on login form — running full login fallback")
        username, password, login_id, sso_email = _credentials()
        page = await perform_full_login(
            page,
            username=username,
            password=password,
            login_id=login_id,
            sso_email=sso_email,
        )

    if await _is_on_sso_picker(page):
        log.info("[SESSION] Existing session page is on SSO picker — selecting first account")
        await _select_first_sso_account(page)
        await page.wait_for_load_state("domcontentloaded")

    current_url = (page.url or "").lower()
    if "m365.cloud.microsoft" in current_url or "login.microsoftonline.com" in current_url:
        log.info("[SESSION] Moving from Microsoft auth page to Foundry home URL")
        await page.goto(FOUNDRY_HOME_URL, wait_until="domcontentloaded")

    on_compass_app = await _is_on_compass_app_page(page)
    if on_compass_app:
        log.info("[SESSION] Already on Compass app page from existing session")
    else:
        if await _is_on_compass_mobile_picker(page):
            log.info("[SESSION] Existing session page on Compass picker — opening Compass Mobile")
            page = await click_compass_mobile_tile(page)
            # Give the new tab time to render the WWID form before probing.
            await page.wait_for_timeout(3_000)

        # Fail fast on unknown landings by steering to the configured health/login URL.
        if not await _is_on_wwid_screen(page):
            log.info("[SESSION] Existing session page unknown — opening login URL: %s", LOGIN_URL)
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await _is_on_wwid_screen(page):
        log.info("[SESSION] Existing session page on WWID screen — submitting login ID")
        _, _, login_id, _ = _credentials()
        page = await enter_wwid(page, login_id)

    return page


_WEBDRIVER_MASK = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


async def _new_context(browser: Browser, *, no_viewport: bool = False, **kwargs) -> "BrowserContext":
    """Create a browser context with automation-detection masking applied."""
    context = await browser.new_context(permissions=[], no_viewport=no_viewport, **kwargs)
    await context.add_init_script(_WEBDRIVER_MASK)
    return context


async def _do_fresh_login(browser: Browser, *, no_viewport: bool = False) -> tuple[BrowserContext, Page]:
    """Create a new context and run a full login from scratch."""
    context = await _new_context(browser, no_viewport=no_viewport)
    page = await context.new_page()
    username, password, login_id, sso_email = _credentials()
    page = await perform_full_login(
        page,
        username=username,
        password=password,
        login_id=login_id,
        sso_email=sso_email,
    )
    return context, page


async def ensure_authenticated_context(browser: Browser, *, no_viewport: bool = False) -> tuple[BrowserContext, Page]:
    """Return (context, page) ready for select_glass_opcode().

    Strategy:
      1. If storage_state.json exists, restore it and navigate to the login URL.
      2. Detect where the restore landed and handle each partial-auth case.
      3. If session is fully expired, fall back to a complete fresh login.
      4. Always re-save state after a successful auth so rolling cookies stay fresh.
    """
    if STORAGE_STATE_PATH.exists():
        log.info("[SESSION] Restoring session from %s", STORAGE_STATE_PATH)
        context = await _new_context(browser, no_viewport=no_viewport, storage_state=str(STORAGE_STATE_PATH))
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        username, password, login_id, sso_email = _credentials()

        if await _is_on_login_page(page):
            log.info("[SESSION] Session expired — performing full login")
            await context.close()
            context, page = await _do_fresh_login(browser, no_viewport=no_viewport)

        elif await _is_on_sso_picker(page):
            log.info("[SESSION] SSO picker detected — selecting account")
            await select_sso_account(page, sso_email)
            page = await click_compass_mobile_tile(page)
            page = await enter_wwid(page, login_id)

        elif await _is_on_compass_mobile_picker(page):
            log.info("[SESSION] Compass Mobile picker detected — clicking tile")
            page = await click_compass_mobile_tile(page)
            page = await enter_wwid(page, login_id)

        elif await _is_on_wwid_screen(page):
            log.info("[SESSION] WWID screen detected — re-entering WWID")
            page = await enter_wwid(page, login_id)

        else:
            log.info("[SESSION] Session restored — already on OpCode list")

    else:
        log.info("[SESSION] No saved session — performing full login")
        context, page = await _do_fresh_login(browser, no_viewport=no_viewport)

    log.info("[SESSION] Saving session state to %s", STORAGE_STATE_PATH)
    await context.storage_state(path=str(STORAGE_STATE_PATH))

    return context, page


async def ensure_attached_context(browser: Browser, *, no_viewport: bool = False) -> tuple[BrowserContext, Page]:
    """Return an existing page from an attached CDP browser session.

    This mode intentionally avoids automating SSO/login and instead reuses
    the user's trusted corporate Edge profile/session.
    """
    context: BrowserContext
    page: Page

    # Preferred behavior: use the primary tab (0-based index) first.
    if browser.contexts and browser.contexts[0].pages:
        context = browser.contexts[0]
        page = context.pages[0]
        score = _score_attached_page(page)
        log.info("[SESSION] Attach selected primary tab[0] URL: %s", page.url)
        if score < 70:
            log.info(
                "[SESSION] Primary tab is not auth/Compass related (score=%s) — opening Foundry home URL",
                score,
            )
            await page.goto(FOUNDRY_HOME_URL, wait_until="domcontentloaded")
    else:
        candidates: list[tuple[int, BrowserContext, Page]] = []
        for ctx in browser.contexts:
            for existing_page in ctx.pages:
                candidates.append((_score_attached_page(existing_page), ctx, existing_page))

        if candidates:
            score, context, page = max(candidates, key=lambda item: item[0])
            log.info("[SESSION] Attach selected tab URL: %s", page.url)
            if score < 70:
                log.info(
                    "[SESSION] Selected tab is not auth/Compass related (score=%s) — opening Foundry home URL",
                    score,
                )
                await page.goto(FOUNDRY_HOME_URL, wait_until="domcontentloaded")
        elif browser.contexts:
            context = browser.contexts[0]
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info("[SESSION] Attach opened login URL in existing context")
        else:
            context = await _new_context(browser, no_viewport=no_viewport)
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log.info("[SESSION] Attach created new context and opened login URL")

    page = await _advance_existing_session_page(page)
    return context, page


async def ensure_profile_context(context: BrowserContext) -> tuple[BrowserContext, Page]:
    """Return a launched persistent-profile context ready for Compass actions."""
    if context.pages:
        page = context.pages[0]
    else:
        page = await context.new_page()

    page = await _advance_existing_session_page(page)
    return context, page
