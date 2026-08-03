# smoke_test_workitem.py — Smoke test for a single MVA work item flow
#
# Usage:
#   Check only (safe — no changes to Compass):
#     .venv\Scripts\python.exe smoke_test_workitem.py 59257306
#
#   Check + create (writes a real work item to Compass):
#     .venv\Scripts\python.exe smoke_test_workitem.py 59257306 --create
#
#   Legacy Selenium backend (fallback):
#     .venv\Scripts\python.exe smoke_test_workitem.py 59257306 --backend selenium
#
# Does NOT read from or write to the Google Sheet.

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from utils.logger import log

from playwright.async_api import async_playwright
from playwright_prototype.config import (
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
    resolve_initial_delay,
    resolve_step_delay,
)
from playwright_prototype.main import process_mva
from playwright_prototype.session import ensure_profile_context
from playwright_prototype.steps import (
    ExistingWorkItemError,
    check_existing_work_item,
    navigate_to_mva as pw_navigate_to_mva,
    warmup_compass as pw_warmup_compass,
)


# ---------------------------------------------------------------------------
# Playwright backend
# ---------------------------------------------------------------------------

async def _playwright_smoke(mva: str, do_create: bool, location: str, action: str) -> None:
    headless = resolve_headless()
    edge_user_data_dir = resolve_edge_user_data_dir()
    edge_profile_directory = resolve_edge_profile_directory()
    step_delay_ms = resolve_step_delay()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(edge_user_data_dir),
            channel="msedge",
            headless=headless,
            chromium_sandbox=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--profile-directory={edge_profile_directory}",
                "--start-maximized",
            ],
            no_viewport=True,
        )

        try:
            _, page = await ensure_profile_context(context)

            initial_delay_ms = resolve_initial_delay()
            if initial_delay_ms > 0:
                await page.wait_for_timeout(initial_delay_ms)

            # Step 1: Warm up Compass
            log.info("[SMOKE] Step 1 — Warming up Compass...")
            await pw_warmup_compass(page)
            log.info("[SMOKE] Compass warm-up complete")

            # Step 2: Navigate to MVA
            log.info("[SMOKE] Step 2 — Navigating to MVA %s...", mva)
            await pw_navigate_to_mva(page, mva)

            # Step 3: Check for existing work item
            log.info("[SMOKE] Step 3 — Checking for existing open glass work item...")
            try:
                await check_existing_work_item(page, mva, "Glass")
                existing = False
            except ExistingWorkItemError:
                existing = True

            if existing:
                log.info("[SMOKE] SKIP path confirmed — work item already exists, nothing to create")
                if do_create:
                    log.info("[SMOKE] --create flag ignored because existing work item was found")
                return

            log.info("[SMOKE] No existing work item found")

            if not do_create:
                log.info("[SMOKE] CHECK ONLY mode — stopping here. Run with --create to test creation.")
                return

            # Step 4: Create work item.
            # process_mva() repeats navigation + precheck internally; we do not
            # duplicate those steps here to avoid unnecessary page churn.
            log.info("[SMOKE] Step 4 — Creating glass work item for %s...", mva)
            try:
                await process_mva(page, mva, location=location, action=action, step_delay_ms=step_delay_ms)
            except ExistingWorkItemError:
                log.info("[SMOKE] SKIP — work item appeared between check and create")
                return
            log.info("[SMOKE] %s", "=" * 50)
            log.info("[SMOKE] SUCCESS — work item created for %s", mva)
            log.info("[SMOKE] %s", "=" * 50)

        finally:
            await context.close()
            log.info("[SMOKE] Browser closed.")


# ---------------------------------------------------------------------------
# Legacy Selenium backend
# ---------------------------------------------------------------------------

def _selenium_smoke(mva: str, do_create: bool) -> None:
    from core.driver_manager import create_driver, quit_driver
    from config.config_loader import get_config
    from flows.LoginFlow import LoginFlow
    from flows.mva_navigation import warmup_compass, navigate_to_mva
    from flows.work_item_flow import check_existing_work_item

    username = os.getenv("GLASS_LOGIN_USERNAME") or get_config("username")
    password = os.getenv("GLASS_LOGIN_PASSWORD") or get_config("password")
    login_id = os.getenv("GLASS_LOGIN_ID") or get_config("login_id")

    driver = create_driver()
    try:
        log.info("[SMOKE] Step 1 — Logging in...")
        login_flow = LoginFlow(driver)
        result = login_flow.login_handler(username, password, login_id)
        if result.get("status") != "ok":
            log.error("[SMOKE] Login failed: %s", result)
            sys.exit(1)
        log.info("[SMOKE] Login OK")

        log.info("[SMOKE] Step 2 — Warming up Compass and navigating to MVA %s...", mva)
        if not warmup_compass(driver):
            log.error("[SMOKE] Compass warm-up failed — aborting")
            sys.exit(1)
        if not navigate_to_mva(driver, mva):
            log.error("[SMOKE] Could not navigate to MVA — aborting")
            sys.exit(1)

        log.info("[SMOKE] Step 3 — Checking for existing open glass work item...")
        has_existing = check_existing_work_item(driver, mva, work_item_type="GLASS")
        log.info("[SMOKE] Result: existing open glass work item = %s", has_existing)

        if has_existing:
            log.info("[SMOKE] SKIP path confirmed — work item already exists, nothing to create")
            if do_create:
                log.info("[SMOKE] --create flag ignored because existing work item was found")
            return

        log.info("[SMOKE] No existing work item found")

        if not do_create:
            log.info("[SMOKE] CHECK ONLY mode — stopping here. Run with --create to test creation.")
            return

        log.info("[SMOKE] Step 4 — Creating glass work item for %s...", mva)
        from flows.work_item_handler import WorkItemConfig, create_work_item_handler
        config = WorkItemConfig(mva=mva, damage_type="Replacement", location="WINDSHIELD")
        handler = create_work_item_handler("GLASS", driver)
        result = handler.create_work_item(config)

        log.info("[SMOKE] %s", "=" * 50)
        if result.get("status") == "created":
            log.info("[SMOKE] SUCCESS — work item created for %s", mva)
        else:
            log.error("[SMOKE] FAILED — result: %s", result)
        log.info("[SMOKE] %s", "=" * 50)

    finally:
        if sys.stdin.isatty():
            try:
                input("\n[SMOKE] Press Enter to close the browser...")
            except EOFError:
                pass
        quit_driver()
        log.info("[SMOKE] Browser closed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test work item creation for a single MVA")
    parser.add_argument("mva", help="MVA number to test")
    parser.add_argument("--create", action="store_true", help="Also create the work item (default: check only)")
    parser.add_argument("--location", default="WS", help="Glass location code (default: WS)")
    parser.add_argument("--action", default="Replace", help="Glass action (default: Replace)")
    parser.add_argument(
        "--backend",
        choices=["selenium", "playwright"],
        default=(os.getenv("GLASS_VERIFY_BACKEND", "playwright").strip().lower() or "playwright"),
        help="Automation backend (default: playwright; set GLASS_VERIFY_BACKEND=selenium for legacy)",
    )
    args = parser.parse_args()

    mva = args.mva.strip()
    log.info("[SMOKE] %s", "=" * 50)
    log.info("[SMOKE] Smoke test starting — MVA: %s", mva)
    log.info("[SMOKE] Mode: %s", "CHECK + CREATE" if args.create else "CHECK ONLY (read-only)")
    log.info("[SMOKE] Backend: %s", args.backend)
    log.info("[SMOKE] %s", "=" * 50)

    if args.backend == "playwright":
        asyncio.run(_playwright_smoke(mva, args.create, args.location, args.action))
    else:
        _selenium_smoke(mva, args.create)


if __name__ == "__main__":
    main()
