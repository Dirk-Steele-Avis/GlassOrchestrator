from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config.config_loader import get_config
from utils.logger import log

from core.driver_manager import create_driver, quit_driver
from flows.LoginFlow import LoginFlow
from flows.mva_navigation import navigate_to_mva, warmup_compass
from flows.work_item_flow import check_existing_work_item

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright_prototype.config import (
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
    resolve_initial_delay,
    resolve_step_delay,
)
from playwright_prototype.session import ensure_profile_context
from playwright_prototype.steps import warmup_compass as pw_warmup_compass
from playwright_prototype.steps import navigate_to_mva as pw_navigate_to_mva

if TYPE_CHECKING:
    from playwright.async_api import Page

# Result constants
RESULT_FOUND = "found"
RESULT_NOT_FOUND = "not_found"
RESULT_NAV_FAILED = "nav_failed"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"


def _load_csv(path: str) -> list[dict]:
    """Return rows from a CSV with at minimum an 'mva' column."""
    with open(path, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if row.get("mva", "").strip()]


def _resolve_row_work_item_type(row: dict, default_type: str) -> str:
    """Resolve per-row type from CSV columns with sane fallbacks.

    Priority:
    1) Explicit ``type`` column
    2) ``action`` column mapping (Repair -> glass repair, else glass damage)
    3) CLI/default type
    """
    explicit_type = (row.get("type") or "").strip()
    if explicit_type:
        return explicit_type

    action = (row.get("action") or "").strip().lower()
    if action == "repair":
        # Repair work items are filed under "Glass Damage" with subtype "Windshield Chip"
        # Tile text contains "windshield chip", not "glass repair"
        return "windshield chip"
    if action in {"replace", "replacement"}:
        return "glass damage"

    return default_type


def _build_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.csv_path:
        rows = _load_csv(args.csv_path)
        targets = [
            (r["mva"].strip(), _resolve_row_work_item_type(r, args.work_item_type))
            for r in rows
        ]
        log.info("[VERIFY] Loaded %d MVA(s) from %s", len(targets), args.csv_path)
        return targets
    return [(args.mva.strip(), args.work_item_type)]


def _capture_selenium_screenshot(driver: Any, label: str, mva: str) -> None:
    """Save a Selenium screenshot to log/ for debugging."""
    try:
        os.makedirs("log", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join("log", f"verify_{label}_{mva}_{timestamp}.png")
        driver.save_screenshot(path)
        log.info("[VERIFY] Screenshot saved: %s", path)
    except Exception as e:
        log.warning("[VERIFY] Could not capture screenshot: %s", e)


async def _capture_playwright_screenshot(page: "Page", label: str, mva: str) -> None:
    """Save a Playwright screenshot to log/ for debugging."""
    try:
        os.makedirs("log", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join("log", f"verify_{label}_{mva}_{timestamp}.png")
        await page.screenshot(path=path, full_page=True)
        log.info("[VERIFY] Screenshot saved: %s", path)
    except Exception as e:
        log.warning("[VERIFY] Could not capture screenshot: %s", e)


def _log_summary(results: list[dict]) -> tuple[int, int]:
    found_count = sum(1 for r in results if r["result"] == RESULT_FOUND)
    not_found_count = sum(1 for r in results if r["result"] == RESULT_NOT_FOUND)
    timeout_count = sum(1 for r in results if r["result"] == RESULT_TIMEOUT)
    failed_count = sum(1 for r in results if r["result"] in {RESULT_NAV_FAILED, RESULT_ERROR, RESULT_TIMEOUT})

    log.info("[VERIFY] %s", "=" * 50)
    log.info("[VERIFY] VERIFICATION SUMMARY - %d MVA(s)", len(results))
    log.info("[VERIFY]   ? Found:     %d", found_count)
    log.info("[VERIFY]   ? Not found: %d", not_found_count)
    log.info("[VERIFY]   ? Timeout:   %d", timeout_count)
    log.info("[VERIFY]   ! Failed:    %d", failed_count)
    log.info("[VERIFY] %s", "=" * 50)
    for r in results:
        status_icon = "+" if r["result"] == RESULT_FOUND else ("-" if r["result"] == RESULT_NOT_FOUND else "!")
        detail = r.get("detail", "")
        detail_suffix = f"  ({detail})" if detail else ""
        log.info("[VERIFY]   %s  %12s  [%s]  ?  %s%s", status_icon, r["mva"], r["type"], r["result"], detail_suffix)
    log.info("[VERIFY] %s", "=" * 50)

    return not_found_count, failed_count


def _run_selenium_verification(args: argparse.Namespace, targets: list[tuple[str, str]], should_pause: bool) -> list[dict]:
    """Legacy Selenium verification flow (retained for fallback; default is now playwright)."""
    username = os.getenv("GLASS_LOGIN_USERNAME") or get_config("username")
    password = os.getenv("GLASS_LOGIN_PASSWORD") or get_config("password")
    login_id = os.getenv("GLASS_LOGIN_ID") or get_config("login_id")

    driver = create_driver()
    results: list[dict] = []

    try:
        try:
            driver.set_page_load_timeout(args.timeout_seconds)
            driver.set_script_timeout(args.timeout_seconds)
        except Exception as e:
            log.warning("[VERIFY] Could not apply Selenium timeouts: %s", e)

        login_flow = LoginFlow(driver)
        login_result = login_flow.login_handler(username, password, login_id)
        if login_result.get("status") != "ok":
            log.error("[VERIFY] Login failed: %s", login_result)
            _capture_selenium_screenshot(driver, "login_failure", "batch")
            return [{"mva": "batch", "type": args.work_item_type, "result": RESULT_ERROR}]
        log.info("[VERIFY] Login OK")

        warmup_timeout = min(30, args.timeout_seconds)
        if not warmup_compass(driver, timeout=warmup_timeout):
            log.error("[VERIFY] Compass warm-up failed - aborting")
            _capture_selenium_screenshot(driver, "warmup_failure", "batch")
            return [{"mva": "batch", "type": args.work_item_type, "result": RESULT_ERROR}]

        for mva, work_item_type in targets:
            log.info("[VERIFY] Checking MVA %s for open '%s' work item...", mva, work_item_type)
            mva_started = time.monotonic()

            def timed_out() -> bool:
                return (time.monotonic() - mva_started) > args.timeout_seconds

            nav_timeout = min(30, args.timeout_seconds)
            if not navigate_to_mva(driver, mva, timeout=nav_timeout):
                log.error("[VERIFY] %s - navigation failed, skipping", mva)
                _capture_selenium_screenshot(driver, "nav_failure", mva)
                results.append({"mva": mva, "type": work_item_type, "result": RESULT_NAV_FAILED})
                continue

            if timed_out():
                log.error("[VERIFY] %s - timed out after %ss before work item check", mva, args.timeout_seconds)
                _capture_selenium_screenshot(driver, "timeout", mva)
                results.append({"mva": mva, "type": work_item_type, "result": RESULT_TIMEOUT})
                continue

            try:
                found = check_existing_work_item(driver, mva, work_item_type=work_item_type)
                if timed_out():
                    log.error("[VERIFY] %s - timed out after %ss during work item check", mva, args.timeout_seconds)
                    _capture_selenium_screenshot(driver, "timeout", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_TIMEOUT})
                    continue

                if found:
                    log.info("[VERIFY] %s - ? FOUND: open '%s' work item confirmed", mva, work_item_type)
                    _capture_selenium_screenshot(driver, "found", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_FOUND})
                else:
                    log.warning("[VERIFY] %s - ? NOT FOUND: no open '%s' work item", mva, work_item_type)
                    _capture_selenium_screenshot(driver, "not_found", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_NOT_FOUND})
            except Exception as e:
                log.error("[VERIFY] %s - error during check: %s", mva, e)
                _capture_selenium_screenshot(driver, "error", mva)
                results.append({"mva": mva, "type": work_item_type, "result": RESULT_ERROR})

    finally:
        if should_pause:
            try:
                input("\n[VERIFY] Press Enter to close the browser...")
            except EOFError:
                pass
        quit_driver()
        log.info("[VERIFY] Browser closed.")

    return results


async def _playwright_find_work_item(page: "Page", work_item_type: str) -> tuple[bool, str]:
    """Return (found, tile_detail) where tile_detail is the matching tile's text (empty if not found)."""
    keyword = work_item_type.strip().lower()

    tiles = page.locator("div[class*='fleet-operations-pwa__scan-record__']").filter(
        has_text=re.compile(r"open", re.I)
    )

    count = await tiles.count()
    for idx in range(count):
        raw = (await tiles.nth(idx).inner_text()).strip()
        if keyword in raw.lower():
            # Condense multi-line tile text to a single readable line
            detail = " | ".join(line.strip() for line in raw.splitlines() if line.strip())
            return True, detail

    return False, ""


async def _run_playwright_verification_async(args: argparse.Namespace, targets: list[tuple[str, str]]) -> list[dict]:
    """Playwright verification backend (default)."""
    results: list[dict] = []
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

            await asyncio.wait_for(pw_warmup_compass(page), timeout=args.timeout_seconds)

            for mva, work_item_type in targets:
                # Poll for UI stability with 1s intervals, 10s timeout total
                log.info("[VERIFY] Settling UI before checking MVA %s (polling every 1s, 10s timeout)...", mva)
                settle_start = time.monotonic()
                settle_timeout = 10.0
                while (time.monotonic() - settle_start) < settle_timeout:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=1_000)
                        log.info("[VERIFY] UI settled")
                        break
                    except (PlaywrightTimeoutError, asyncio.TimeoutError):
                        elapsed = time.monotonic() - settle_start
                        if elapsed < settle_timeout:
                            log.debug("[VERIFY] UI not yet idle (%.1fs elapsed), polling again...", elapsed)
                            continue
                        else:
                            log.warning("[VERIFY] %s - UI settle timeout after %.1fs, proceeding", mva, elapsed)
                            break

                if step_delay_ms > 0:
                    await page.wait_for_timeout(step_delay_ms)

                log.info("[VERIFY] Checking MVA %s for open '%s' work item...", mva, work_item_type)
                started = time.monotonic()

                try:
                    await asyncio.wait_for(pw_navigate_to_mva(page, mva), timeout=args.timeout_seconds)
                    # Give Compass UI a short settle window after route/navigation churn.
                    if step_delay_ms > 0:
                        await page.wait_for_timeout(step_delay_ms)
                except asyncio.TimeoutError:
                    log.error("[VERIFY] %s - timed out after %ss during navigation", mva, args.timeout_seconds)
                    await _capture_playwright_screenshot(page, "timeout", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_TIMEOUT})
                    continue
                except (PlaywrightTimeoutError, Exception) as exc:
                    log.error("[VERIFY] %s - navigation failed, skipping: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "nav_failure", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_NAV_FAILED})
                    continue

                elapsed = time.monotonic() - started
                remaining = max(1.0, args.timeout_seconds - elapsed)

                try:
                    found, tile_detail = await asyncio.wait_for(_playwright_find_work_item(page, work_item_type), timeout=remaining)
                    if found:
                        log.info("[VERIFY] %s - ? FOUND: open '%s' work item confirmed", mva, work_item_type)
                        if tile_detail:
                            log.info("[VERIFY] %s -   detail: %s", mva, tile_detail)
                        await _capture_playwright_screenshot(page, "found", mva)
                        results.append({"mva": mva, "type": work_item_type, "result": RESULT_FOUND, "detail": tile_detail})
                    else:
                        log.warning("[VERIFY] %s - ? NOT FOUND: no open '%s' work item", mva, work_item_type)
                        await _capture_playwright_screenshot(page, "not_found", mva)
                        results.append({"mva": mva, "type": work_item_type, "result": RESULT_NOT_FOUND, "detail": ""})
                except asyncio.TimeoutError:
                    log.error("[VERIFY] %s - timed out after %ss during work item check", mva, args.timeout_seconds)
                    await _capture_playwright_screenshot(page, "timeout", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_TIMEOUT, "detail": ""})
                except Exception as exc:
                    log.error("[VERIFY] %s - error during check: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "error", mva)
                    results.append({"mva": mva, "type": work_item_type, "result": RESULT_ERROR, "detail": ""})
        finally:
            await context.close()
            log.info("[VERIFY] Browser closed.")

    return results


def _run_playwright_verification(args: argparse.Namespace, targets: list[tuple[str, str]], should_pause: bool) -> list[dict]:
    results = asyncio.run(_run_playwright_verification_async(args, targets))
    if should_pause:
        try:
            input("\n[VERIFY] Press Enter to continue...")
        except EOFError:
            pass
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify that open work items of a given type exist for one or more MVAs. Read-only."
    )
    parser.add_argument("mva", nargs="?", default=None, help="Single target MVA (omit when using --csv)")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Path to CSV file with 'mva' column and optional 'type' column")
    parser.add_argument(
        "--type",
        dest="work_item_type",
        default="glass damage",
        help=(
            "Work item type keyword to match in tile text (default: 'glass damage'). "
            "Examples: 'glass damage', 'Windshield Crack', 'Windshield Chip', 'Side/Rear Window Damage'"
        ),
    )
    parser.add_argument("--no-pause", action="store_true", help="Deprecated: no-op kept for backward compatibility (default is non-blocking)")
    parser.add_argument("--pause", action="store_true", help="Prompt for Enter before closing the browser (opt-in)")
    parser.add_argument("--timeout-seconds", type=int, default=90, help="Per-MVA timeout in seconds for verification flow (default: 90)")
    parser.add_argument(
        "--backend",
        choices=["selenium", "playwright"],
        default=(os.getenv("GLASS_VERIFY_BACKEND", "playwright").strip().lower() or "playwright"),
        help="Verification backend (default: playwright; set GLASS_VERIFY_BACKEND=selenium to use legacy backend)",
    )

    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than 0")
    if not args.mva and not args.csv_path:
        parser.error("Provide a single MVA positional argument or --csv <path>")
    if args.mva and args.csv_path:
        parser.error("Provide either a single MVA or --csv, not both")

    agentic_env = os.getenv("GLASS_AGENTIC", "").strip().lower() in {"1", "true", "yes"}
    should_pause = sys.stdin.isatty() and args.pause and not agentic_env
    targets = _build_targets(args)

    log.info("[VERIFY] %s", "=" * 50)
    log.info("[VERIFY] Work item verification - %d MVA(s)", len(targets))
    log.info("[VERIFY] Default type filter: %s", args.work_item_type)
    log.info("[VERIFY] Backend: %s", args.backend)
    log.info("[VERIFY] %s", "=" * 50)

    if args.backend == "playwright":
        results = _run_playwright_verification(args, targets, should_pause)
    else:
        results = _run_selenium_verification(args, targets, should_pause)

    not_found_count, failed_count = _log_summary(results)
    if not_found_count > 0 or failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

