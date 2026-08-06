from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import date, datetime
import inspect
import os
import re
import subprocess
import sys
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

if TYPE_CHECKING:
    from playwright.async_api import Page

# Result constants
RESULT_CLOSED = "closed"
RESULT_NOT_FOUND = "not_found"
RESULT_NAV_FAILED = "nav_failed"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"


async def _debug_hold_if_configured(page: "Page", args: argparse.Namespace, mva: str, reason: str) -> None:
    """Keep the page open for manual debugging when configured.

    This is useful for investigating transient UI states before the browser closes.
    """
    hold_seconds = int(getattr(args, "debug_hold_seconds", 0) or 0)
    if hold_seconds <= 0:
        return
    log.warning(
        "[CLOSE] %s - debug hold active for %ss after %s. Capture screenshots now.",
        mva,
        hold_seconds,
        reason,
    )
    await page.wait_for_timeout(hold_seconds * 1000)

_GLASS_PATTERN = re.compile(r"glass|windshield|crack|chip|window", re.I)
_PM_PATTERN = re.compile(r"PM", re.I)

COMPLAINT_TYPE_PATTERNS = {
    "Glass": _GLASS_PATTERN,
    "PM": _PM_PATTERN,
}


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
        reader = csv.DictReader(f)
        if not reader.fieldnames or "mva" not in reader.fieldnames:
            log.error("[CLOSE] CSV missing required 'mva' column: %s", path)
            sys.exit(1)
        return [row for row in reader if row.get("mva", "").strip()]


def _load_sheet_rows() -> list[dict]:
    """Return rows from the configured Glass sheet tab."""
    try:
        import gspread  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError:
        log.error("[CLOSE] Missing dependency 'gspread'. Use project venv and install requirements.")
        sys.exit(1)

    service_account_json = str(get_config("service_account_json", "Service_account.json")).strip()
    spreadsheet_id = str(get_config("spreadsheet_id", "")).strip()
    sheet_name = str(get_config("sheet_name", "GlassClaims")).strip() or "GlassClaims"

    if not spreadsheet_id:
        log.error("[CLOSE] Missing spreadsheet_id in config.")
        sys.exit(1)
    if not service_account_json:
        log.error("[CLOSE] Missing service_account_json in config.")
        sys.exit(1)
    if not os.path.exists(service_account_json):
        log.error("[CLOSE] Service account file not found: %s", service_account_json)
        sys.exit(1)

    try:
        gc = gspread.service_account(filename=service_account_json)
        sh = gc.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
        values = ws.get_all_values()
        if not values:
            return []

        headers = [str(header).strip() for header in values[0]]
        active_columns = [(idx, header) for idx, header in enumerate(headers) if header]
        rows: list[dict] = []
        for raw_row in values[1:]:
            row = {
                header: (raw_row[idx].strip() if idx < len(raw_row) else "")
                for idx, header in active_columns
            }
            rows.append(row)
        return rows
    except Exception as exc:
        log.error("[CLOSE] Failed to read sheet '%s' (%s): %s", sheet_name, spreadsheet_id, exc)
        sys.exit(1)


def _normalize_sheet_type(raw_type: str, valid_types: list[str]) -> str:
    """Map sheet type/damage values into supported complaint types."""
    value = (raw_type or "").strip()
    if value in valid_types:
        return value

    lowered = value.lower()
    if lowered in {"replacement", "repair", "glass"}:
        return "Glass"
    if lowered.startswith("pm"):
        return "PM"

    return ""


def _normalize_sheet_mva(raw_mva: str) -> str:
    """Normalize a sheet MVA to the required 9-digit search form."""
    value = (raw_mva or "").strip()
    if len(value) == 8 and value.isdigit():
        return f"0{value}"
    if len(value) == 9 and value.isdigit():
        return value
    return ""


def _parse_inventory_date(value: str) -> date | None:
    """Parse an Inventory Date cell in strict MM/DD/YYYY format."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        return None


def _build_targets_from_sheet(max_rows: int | None = None) -> list[dict]:
    """Build close targets from the configured Glass sheet tab."""
    rows = _load_sheet_rows()
    valid_types = _get_valid_complaint_types()
    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()
    today = date.today()

    for row in rows:
        inventory_raw = str(row.get("Inventory Date") or "").strip()
        inventory_date = _parse_inventory_date(inventory_raw)
        if inventory_raw and inventory_date is None:
            log.error("[CLOSE] Invalid Inventory Date format for MVA %s: %s (expected MM/DD/YYYY)", row.get("MVA") or row.get("mva") or "", inventory_raw)
            sys.exit(1)
        if inventory_date != today:
            continue

        raw_mva = str(row.get("MVA") or row.get("mva") or "").strip()
        if not raw_mva:
            continue

        mva = _normalize_sheet_mva(raw_mva)
        if not mva:
            log.error("[CLOSE] Invalid MVA format in sheet: %s (expected 8 or 9 digits)", raw_mva)
            sys.exit(1)

        raw_type = str(row.get("Type") or row.get("Damage Type") or "Glass")
        complaint_type = _normalize_sheet_type(raw_type, valid_types)
        if not complaint_type:
            log.warning("[CLOSE] Sheet row for MVA %s has unsupported type '%s' — skipping", mva, raw_type)
            continue

        key = (mva, complaint_type)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"mva": mva, "complaint_type": complaint_type})
        if max_rows is not None and len(targets) >= max_rows:
            break

    if not targets:
        log.error("[CLOSE] No valid MVA targets found in configured sheet.")
        sys.exit(1)

    if max_rows is None:
        log.info("[CLOSE] Loaded %d MVA(s) from sheet source", len(targets))
    else:
        log.info("[CLOSE] Loaded %d MVA(s) from sheet source (max_rows=%d)", len(targets), max_rows)
    return targets


def _build_targets_from_mvas(raw_mvas: str) -> list[dict]:
    """Build close targets from a caller-provided list of MVAs."""
    tokens = [token.strip() for token in re.split(r"[,\s]+", raw_mvas or "") if token.strip()]
    if not tokens:
        log.error("[CLOSE] --mvas was provided but no MVA values were parsed.")
        sys.exit(1)

    targets: list[dict] = []
    seen: set[str] = set()
    for raw_mva in tokens:
        mva = _normalize_sheet_mva(raw_mva)
        if not mva:
            log.error("[CLOSE] Invalid MVA format in --mvas: %s (expected 8 or 9 digits)", raw_mva)
            sys.exit(1)
        if mva in seen:
            continue
        seen.add(mva)
        targets.append({"mva": mva, "complaint_type": "Glass"})

    log.info("[CLOSE] Loaded %d MVA(s) from explicit --mvas list", len(targets))
    return targets


def _build_targets(args: argparse.Namespace) -> list[dict]:
    """Build list of targets from explicit MVAs or configured Glass sheet."""
    explicit_mvas = str(getattr(args, "mvas", "") or "").strip()
    if explicit_mvas:
        return _build_targets_from_mvas(explicit_mvas)

    max_rows = getattr(args, "max_rows", None)
    return _build_targets_from_sheet(max_rows=max_rows)


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


async def _ensure_live_page(context, page: "Page", mva: str) -> "Page":
    """Return a usable page, rebinding to another context page when needed."""
    try:
        if page:
            closed = page.is_closed()
            if inspect.isawaitable(closed):
                closed = await closed
            if not closed:
                return page
    except Exception:
        pass

    live_pages = []
    for candidate in context.pages:
        try:
            closed = candidate.is_closed()
            if inspect.isawaitable(closed):
                closed = await closed
            if not closed:
                live_pages.append(candidate)
        except Exception:
            continue
    if live_pages:
        rebound = live_pages[-1]
        log.warning("[CLOSE] %s - current page closed; rebinding to existing tab: %s", mva, rebound.url)
        return rebound

    rebound = await context.new_page()
    await rebound.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.warning("[CLOSE] %s - current page closed; opened a new tab at login URL", mva)
    return rebound


async def _playwright_close_work_item(page: "Page", mva: str, complaint_type: str) -> tuple[str, str]:
    """Find the open work item tile of the specified type, expand it, and mark it complete.
    
    Verifies the tile's actual complaint type matches the expected type before attempting close.

    Returns (result_constant, tile_detail_text).
    """
    pattern = COMPLAINT_TYPE_PATTERNS.get(complaint_type, _GLASS_PATTERN)
    
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
    debug_hold_seconds = int(getattr(args, "debug_hold_seconds", 0) or 0)
    log.info(
        "[CLOSE] Runtime config | profile=%s | headless=%s | initial_delay_ms=%s | step_delay_ms=%s | timeout_seconds=%s | debug_hold_seconds=%s",
        edge_profile_directory,
        headless,
        initial_delay_ms,
        step_delay_ms,
        args.timeout_seconds,
        debug_hold_seconds,
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
                page = await _ensure_live_page(context, page, mva)
                
                log.info("[CLOSE] Settling UI before closing MVA %s (Type: %s, polling every 1s, 10s timeout)...", mva, complaint_type)
                settle_start = time.monotonic()
                settle_timeout = 10.0
                while (time.monotonic() - settle_start) < settle_timeout:
                    try:
                        page = await _ensure_live_page(context, page, mva)
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
                    page = await asyncio.wait_for(pw_navigate_to_mva(page, mva), timeout=args.timeout_seconds)
                    landing_url = page.url
                    log.info("[CLOSE] %s - navigation landed at URL: %s", mva, landing_url)
                    _validate_post_navigation_url(landing_url, mva)
                    if step_delay_ms > 0:
                        await page.wait_for_timeout(step_delay_ms)
                except asyncio.TimeoutError:
                    log.error("[CLOSE] %s - timed out after %ss during navigation", mva, args.timeout_seconds)
                    await _capture_playwright_screenshot(page, "timeout", mva)
                    await _debug_hold_if_configured(page, args, mva, "navigation timeout")
                    results.append({"mva": mva, "result": RESULT_TIMEOUT, "detail": "navigation"})
                    continue
                except (PlaywrightTimeoutError, Exception) as exc:
                    log.error("[CLOSE] %s - navigation failed, skipping: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "nav_failure", mva)
                    await _debug_hold_if_configured(page, args, mva, "navigation failure")
                    results.append({"mva": mva, "result": RESULT_NAV_FAILED, "detail": ""})
                    continue

                elapsed = time.monotonic() - started
                close_timeout = float(args.timeout_seconds)
                log.info("[CLOSE] %s - navigation completed in %.1fs; close timeout budget=%ss", mva, elapsed, args.timeout_seconds)

                try:
                    result, detail = await asyncio.wait_for(
                        _playwright_close_work_item(page, mva, complaint_type), timeout=close_timeout
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
                    await _debug_hold_if_configured(page, args, mva, "close timeout")
                    results.append({"mva": mva, "result": RESULT_TIMEOUT, "detail": "close"})
                except Exception as exc:
                    log.error("[CLOSE] %s - error during close: %s", mva, exc)
                    await _capture_playwright_screenshot(page, "error", mva)
                    await _debug_hold_if_configured(page, args, mva, "close error")
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
    timeout_nav_count = sum(
        1 for r in results if r["result"] == RESULT_TIMEOUT and r.get("detail") == "navigation"
    )
    timeout_close_count = sum(
        1 for r in results if r["result"] == RESULT_TIMEOUT and r.get("detail") == "close"
    )
    timeout_other_count = timeout_count - timeout_nav_count - timeout_close_count
    failed_count = sum(1 for r in results if r["result"] in {RESULT_NAV_FAILED, RESULT_ERROR, RESULT_TIMEOUT})

    log.info("[CLOSE] %s", "=" * 50)
    log.info("[CLOSE] CLOSE SUMMARY - %d MVA(s)", len(results))
    log.info("[CLOSE]   + Closed:    %d", closed_count)
    log.info("[CLOSE]   - Not found: %d", not_found_count)
    log.info("[CLOSE]   - Timeout:   %d", timeout_count)
    if timeout_count > 0:
        log.info(
            "[CLOSE]     Timeout breakdown: navigation=%d close=%d other=%d",
            timeout_nav_count,
            timeout_close_count,
            timeout_other_count,
        )
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
        description="Close open glass work items for MVAs loaded from the configured Glass sheet."
    )
    parser.add_argument("--no-pause", action="store_true", help="Deprecated: no-op kept for backward compatibility")
    parser.add_argument("--pause", action="store_true", help="Prompt for Enter before closing the browser (opt-in)")
    parser.add_argument("--timeout-seconds", type=int, default=120, help="Per-phase timeout in seconds for navigation and close (default: 120)")
    parser.add_argument("--debug-hold-seconds", type=int, default=0, help="Keep browser open this many seconds after failures/timeouts for manual debugging")
    parser.add_argument("--max-rows", type=int, default=None, help="Cap sheet-sourced targets to the first N eligible rows")
    parser.add_argument("--mvas", type=str, default="", help="Comma/space-separated MVA list to process explicitly (overrides sheet source)")

    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than 0")
    if args.debug_hold_seconds < 0:
        parser.error("--debug-hold-seconds must be 0 or greater")
    if args.max_rows is not None and args.max_rows <= 0:
        parser.error("--max-rows must be greater than 0")

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
