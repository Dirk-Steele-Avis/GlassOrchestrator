from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import csv
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING

from utils.logger import log

from config.config_loader import get_config
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright_prototype.config import (
    LOGIN_URL,
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
    resolve_initial_delay,
    resolve_step_delay,
)
from playwright_prototype.session import ensure_profile_context
from playwright_prototype.steps import warmup_compass as pw_warmup_compass
from playwright_prototype.steps import navigate_to_mva as pw_navigate_to_mva
from playwright_prototype.steps import open_glass_work_item_tile, complete_glass_work_item
from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS

if TYPE_CHECKING:
    from playwright.async_api import Page

# Result constants
RESULT_CLOSED = "closed"
RESULT_NOT_FOUND = "not_found"
RESULT_NAV_FAILED = "nav_failed"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"


def _is_edge_running() -> bool:
    """Return True if any msedge.exe process is currently running."""
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
        capture_output=True, text=True
    )
    return "msedge.exe" in result.stdout


def _get_valid_complaint_types() -> list[str]:
    """Load valid complaint types from config."""
    return get_config("valid_complaint_types", ["Glass", "PM"])


def _validate_post_navigation_url(url: str, mva: str) -> None:
    """Fail fast if navigation lands on an auth or non-Foundry page."""
    lowered = (url or "").lower()
    if not lowered:
        raise RuntimeError(f"[CLOSE] {mva} - navigation landed on empty URL")
    if "login.microsoftonline.com" in lowered or "m365.cloud.microsoft" in lowered:
        raise RuntimeError(f"[CLOSE] {mva} - navigation landed on auth page: {url}")
    if "palantirfoundry.com" not in lowered:
        raise RuntimeError(f"[CLOSE] {mva} - navigation landed off Foundry domain: {url}")


def _load_csv(path: str) -> list[dict]:
    """Return rows from a CSV with at minimum an 'mva' column."""
    if not os.path.exists(path):
        log.error("[CLOSE] CSV file not found: %s", path)
        sys.exit(1)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(line for line in f if not line.startswith("#"))
        if not reader.fieldnames or "mva" not in reader.fieldnames:
            log.error("[CLOSE] CSV missing required 'mva' column: %s", path)
            sys.exit(1)
        return [row for row in reader if row.get("mva", "").strip()]


def _build_targets(args: argparse.Namespace) -> list[dict]:
    """Build list of (mva, complaint_type) targets from CLI args.

    Returns list of dicts with 'mva' and 'complaint_type' keys.
    """
    if args.csv_path:
        rows = _load_csv(args.csv_path)
        targets = []
        valid_types = _get_valid_complaint_types()

        for i, row in enumerate(rows, start=2):
            mva = row.get("mva", "").strip()
            if not mva:
                log.warning("[CLOSE] Row %d: empty MVA, skipping", i)
                continue

            complaint_type = row.get("Type", "").strip()
            if not complaint_type:
                log.error("[CLOSE] Row %d: missing 'Type' column for MVA %s", i, mva)
                sys.exit(1)

            if complaint_type not in valid_types:
                log.error("[CLOSE] Row %d: invalid Type '%s' for MVA %s — must be one of: %s",
                         i, complaint_type, mva, ", ".join(valid_types))
                sys.exit(1)

            targets.append({"mva": mva, "complaint_type": complaint_type})

        if not targets:
            log.error("[CLOSE] No valid MVAs found in %s", args.csv_path)
            sys.exit(1)
        log.info("[CLOSE] Loaded %d MVA(s) from %s", len(targets), args.csv_path)
        return targets

    return [{"mva": args.mva.strip(), "complaint_type": args.complaint_type or "Glass"}]


async def _capture_playwright_screenshot(page: "Page", label: str, mva: str) -> None:
    """Save a Playwright screenshot to log/ for debugging."""
    try:
        os.makedirs("log", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join("log", f"close_{label}_{mva}_{timestamp}.png")
        await page.screenshot(path=path, full_page=True)
        log.info("[CLOSE] Screenshot saved: %s", path)
    except Exception as e:
        log.warning("[CLOSE] Could not capture screenshot: %s", e)


async def _playwright_close_work_item(page: "Page", mva: str, complaint_type: str) -> tuple[str, str]:
    """Find the open work item tile of the specified type, expand it, and mark it complete.

    Verifies the tile's actual complaint type matches the expected type before attempting close.

    Returns (result_constant, tile_detail_text).
    """
    pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, COMPLAINT_TYPE_PATTERNS["Glass"])

    tiles = page.locator("div[class*='fleet-operations-pwa__scan-record__']").filter(
        has=page.locator("[class*='fleet-operations-pwa__scan-record-header-title-right__']",
                         has_text=re.compile(r"^open$", re.I))
    )

    count = await tiles.count()
    if count == 0:
        return RESULT_NOT_FOUND, ""

    for idx in range(count):
        tile = tiles.nth(idx)
        raw = (await tile.inner_text()).strip()
        detail = " | ".join(line.strip() for line in raw.splitlines() if line.strip())

        complaints_elem = tile.locator("[class*='fleet-operations-pwa__left__']").filter(
            has_text=re.compile(r"complaints\s*:", re.I)
        )
        if await complaints_elem.count() > 0:
            complaints_text = (await complaints_elem.first.inner_text()).strip()
            log.info("[CLOSE] %s - tile %d complaints row: %s", mva, idx, complaints_text)

            match = re.search(r"complaints\s*:\s*(.+)", complaints_text, re.I)
            if match:
                actual_complaint = match.group(1).strip()
                if not pattern.search(actual_complaint):
                    log.info("[CLOSE] %s - tile %d type mismatch (expected %s, got %s) — skipping", mva, idx, complaint_type, actual_complaint)
                    continue

        await open_glass_work_item_tile(page, mva, complaint_type)
        await complete_glass_work_item(page, mva, type=complaint_type)
        return RESULT_CLOSED, detail

    log.warning("[CLOSE] %s - no open %s tile found among %d open tile(s)", mva, complaint_type, count)
    return RESULT_NOT_FOUND, ""


async def _run_playwright_close_async(args: argparse.Namespace, targets: list[dict]) -> list[dict]:
    """Playwright close backend.

    Args:
        args: argparse.Namespace with timeout_seconds
        targets: list of dicts with 'mva' and 'complaint_type' keys
    """
    results: list[dict] = []
    headless = resolve_headless()
    edge_user_data_dir = resolve_edge_user_data_dir()
    edge_profile_directory = resolve_edge_profile_directory()
    initial_delay_ms = resolve_initial_delay()
    step_delay_ms = resolve_step_delay()

    log.info("[CLOSE] %s", "=" * 50)
    log.info("[CLOSE] Close workflow - %d MVA(s)", len(targets))
    log.info("[CLOSE] Runtime config | login_url=%s", LOGIN_URL)
    log.info(
        "[CLOSE] Runtime config | profile=%s | headless=%s | initial_delay_ms=%s | step_delay_ms=%s | timeout_seconds=%s",
        edge_profile_directory,
        headless,
        initial_delay_ms,
        step_delay_ms,
        args.timeout_seconds,
    )
    log.info("[CLOSE] %s", "=" * 50)

    # Edge must not be running when using launch_persistent_context —
    # the user-data-dir lock prevents a second instance from starting.
    # If residual background processes remain after the user closes the UI,
    # kill them automatically and wait briefly before launching.
    if _is_edge_running():
        log.warning("[CLOSE] Edge processes detected — killing residual processes before launch...")
        subprocess.run(["taskkill", "/F", "/IM", "msedge.exe", "/T"],
                       capture_output=True, text=True)
        time.sleep(2)
        if _is_edge_running():
            log.error(
                "[CLOSE] Microsoft Edge is still running after kill attempt. "
                "Please close all Edge windows manually and try again."
            )
            sys.exit(1)
        log.info("[CLOSE] Edge processes cleared — proceeding with launch.")

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

            if initial_delay_ms > 0:
                await page.wait_for_timeout(initial_delay_ms)

            await asyncio.wait_for(pw_warmup_compass(page), timeout=args.timeout_seconds)

            for target in targets:
                mva = target["mva"]
                complaint_type = target["complaint_type"]

                log.info("[CLOSE] %s", "-" * 40)
                log.info("[CLOSE] MVA %s  |  type=%s", mva, complaint_type)
                log.info("[CLOSE] Settling UI (polling every 1s, 10s timeout)...")
                settle_start = time.monotonic()
                settle_timeout = 10.0
                while (time.monotonic() - settle_start) < settle_timeout:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=1_000)
                        log.info("[CLOSE] UI settled")
                        break
                    except (PlaywrightTimeoutError, asyncio.TimeoutError):
                        elapsed = time.monotonic() - settle_start
                        if elapsed < settle_timeout:
                            log.debug("[CLOSE] UI not yet idle (%.1fs elapsed), polling again...", elapsed)
                            continue
                        else:
                            log.warning("[CLOSE] %s - UI settle timeout after %.1fs, proceeding", mva, elapsed)
                            break

                if step_delay_ms > 0:
                    await page.wait_for_timeout(step_delay_ms)

                log.info("[CLOSE] Closing open %s work item for MVA %s...", complaint_type, mva)
                started = time.monotonic()

                try:
                    # If the browser landed on a deep-link work item URL from the previous
                    # MVA, the MVA input field won't be present. Navigate back to the base
                    # health page first so _enter_mva can find the input field.
                    if "/viewWorkItem/" in page.url or "/workItem/" in page.url:
                        log.info("[CLOSE] %s - returning to base health page before navigation", mva)
                        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(step_delay_ms or 1000)

                    log.info("[CLOSE] %s - navigating to MVA", mva)
                    await asyncio.wait_for(pw_navigate_to_mva(page, mva), timeout=args.timeout_seconds)
                    landing_url = page.url
                    log.info("[CLOSE] %s - navigation landed at URL: %s", mva, landing_url)
                    _validate_post_navigation_url(landing_url, mva)
                    if step_delay_ms > 0:
                        await page.wait_for_timeout(step_delay_ms)
                except asyncio.TimeoutError:
                    log.error("[CLOSE] %s - timed out after %ss during navigation", mva, args.timeout_seconds)
                    await _capture_playwright_screenshot(page, "timeout", mva)
                    results.append({"mva": mva, "result": RESULT_TIMEOUT, "detail": ""})
                    continue
                except (PlaywrightTimeoutError, Exception) as exc:
                    log.error("[CLOSE] %s - navigation failed, skipping: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "nav_failure", mva)
                    results.append({"mva": mva, "result": RESULT_NAV_FAILED, "detail": ""})
                    continue

                elapsed = time.monotonic() - started
                remaining = max(1.0, args.timeout_seconds - elapsed)

                try:
                    result, detail = await asyncio.wait_for(
                        _playwright_close_work_item(page, mva, complaint_type), timeout=remaining
                    )
                    if result == RESULT_CLOSED:
                        log.info("[CLOSE] %s - CLOSED: %s work item marked complete", mva, complaint_type)
                        if detail:
                            log.info("[CLOSE] %s -   detail: %s", mva, detail)
                        await _capture_playwright_screenshot(page, "closed", mva)
                    elif result == RESULT_NOT_FOUND:
                        log.warning("[CLOSE] %s - NOT FOUND: no open %s work item to close", mva, complaint_type)
                        await _capture_playwright_screenshot(page, "not_found", mva)
                    results.append({"mva": mva, "result": result, "detail": detail})
                except asyncio.TimeoutError:
                    log.error("[CLOSE] %s - timed out after %ss during close", mva, args.timeout_seconds)
                    await _capture_playwright_screenshot(page, "timeout", mva)
                    results.append({"mva": mva, "result": RESULT_TIMEOUT, "detail": ""})
                except Exception as exc:
                    log.error("[CLOSE] %s - error during close: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "error", mva)
                    results.append({"mva": mva, "result": RESULT_ERROR, "detail": ""})
        finally:
            await context.close()
            log.info("[CLOSE] Browser closed.")

    return results


def _run_playwright_close(args: argparse.Namespace, targets: list[dict], should_pause: bool) -> list[dict]:
    results = asyncio.run(_run_playwright_close_async(args, targets))
    if should_pause:
        try:
            input("\n[CLOSE] Press Enter to continue...")
        except EOFError:
            pass
    return results


def _log_summary(results: list[dict]) -> tuple[int, int]:
    closed_count = sum(1 for r in results if r["result"] == RESULT_CLOSED)
    not_found_count = sum(1 for r in results if r["result"] == RESULT_NOT_FOUND)
    timeout_count = sum(1 for r in results if r["result"] == RESULT_TIMEOUT)
    failed_count = sum(1 for r in results if r["result"] in {RESULT_NAV_FAILED, RESULT_ERROR, RESULT_TIMEOUT})

    log.info("[CLOSE] %s", "=" * 50)
    log.info("[CLOSE] CLOSE SUMMARY - %d MVA(s)", len(results))
    log.info("[CLOSE]   + Closed:    %d", closed_count)
    log.info("[CLOSE]   - Not found: %d", not_found_count)
    log.info("[CLOSE]   - Timeout:   %d", timeout_count)
    log.info("[CLOSE]   ! Failed:    %d", failed_count)
    log.info("[CLOSE] %s", "=" * 50)
    for r in results:
        status_icon = "+" if r["result"] == RESULT_CLOSED else ("-" if r["result"] == RESULT_NOT_FOUND else "!")
        detail = r.get("detail", "")
        detail_suffix = f"  ({detail})" if detail else ""
        log.info("[CLOSE]   %s  %12s  [%s]%s", status_icon, r["mva"], r["result"], detail_suffix)
    log.info("[CLOSE] %s", "=" * 50)

    return not_found_count, failed_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Close open glass work items for one or more MVAs."
    )
    parser.add_argument("mva", nargs="?", default=None, help="Single target MVA (omit when using --csv)")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Path to CSV file with 'mva' and 'Type' columns")
    parser.add_argument("--type", dest="complaint_type", default=None, help="Complaint type for single MVA (e.g., Glass, PM) — optional, defaults to Glass")
    parser.add_argument("--no-pause", action="store_true", help="Deprecated: no-op kept for backward compatibility")
    parser.add_argument("--pause", action="store_true", help="Prompt for Enter before closing the browser (opt-in)")
    parser.add_argument("--timeout-seconds", type=int, default=90, help="Per-MVA timeout in seconds (default: 90)")

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

    log.info("[CLOSE] %s", "=" * 50)
    log.info("[CLOSE] Glass work item close — %d MVA(s)", len(targets))
    log.info("[CLOSE] %s", "=" * 50)

    results = _run_playwright_close(args, targets, should_pause)

    not_found_count, failed_count = _log_summary(results)
    if not_found_count > 0 or failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
