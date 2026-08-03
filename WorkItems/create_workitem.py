# WorkItems/create_workitem.py — Batch work item creation with persistent session
#
# Usage:
#   Create work items from CSV in a single session:
#     .venv\Scripts\python.exe WorkItems\create_workitem.py --csv WorkItems\create_workitem.csv
#
#   Create single MVA (Glass only):
#     .venv\Scripts\python.exe WorkItems\create_workitem.py 59257306
#

from __future__ import annotations

import argparse
import asyncio
import csv
import subprocess
import sys
import time
from pathlib import Path

# Allow imports from repo root (playwright_prototype, utils, config packages)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import log

VALID_GLASS_LOCATIONS = {
    "WS", "WINDSHIELD", "FRONT",
    "FLD", "FRD", "RLD", "RRD",
    "FLV", "FRV",
    "BW",
    "SR",
    "RLQ", "RRQ", "FRW",
}

from playwright.async_api import async_playwright
from config.config_loader import get_config
from playwright_prototype.config import (
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
    resolve_initial_delay,
    resolve_step_delay,
)
from playwright_prototype.session import ensure_profile_context
from playwright_prototype.steps import (
    ExistingWorkItemError,
    check_existing_work_item,
    click_add_work_item,
    complete_mileage_dialog,
    confirm_completion,
    create_work_item,
    handle_complaint_dialog,
    navigate_to_mva as pw_navigate_to_mva,
    select_opcode,
    warmup_compass as pw_warmup_compass,
)


def _is_edge_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
        capture_output=True, text=True, check=False,
    )
    return "msedge.exe" in result.stdout


def _resolve_row_work_item_action(row: dict, default_action: str) -> str:
    if "action" in row and row["action"].strip():
        action = row["action"].strip()
        if action.lower() in ["replace", "repair"]:
            return action.lower().capitalize()
    return default_action


def _build_create_targets(args) -> list[dict]:
    """Build list of (mva, type, location, action) targets from CLI args."""
    targets = []

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            log.error("[CREATE] CSV file not found: %s", csv_path)
            sys.exit(1)

        valid_types = get_config("valid_complaint_types", ["Glass", "PM"])

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(line for line in f if not line.startswith("#"))
            if not reader.fieldnames or "mva" not in reader.fieldnames:
                log.error("[CREATE] CSV missing required 'mva' column")
                sys.exit(1)

            default_action = args.action or "Replace"
            for i, row in enumerate(reader, start=2):
                mva = (row.get("mva") or "").strip()
                if not mva:
                    log.warning("[CREATE] Row %d: blank mva — skipping", i)
                    continue

                work_type = (row.get("Type") or "Glass").strip()
                if work_type not in valid_types:
                    log.error(
                        "[CREATE] Row %d: invalid Type '%s' for MVA %s — must be one of: %s",
                        i, work_type, mva, ", ".join(valid_types),
                    )
                    sys.exit(1)

                if work_type == "Glass":
                    location = (row.get("location") or "").strip()
                    if not location:
                        log.error("[CREATE] Row %d: Glass row for MVA %s is missing location", i, mva)
                        sys.exit(1)
                    if location.upper() not in VALID_GLASS_LOCATIONS:
                        log.error(
                            "[CREATE] Row %d: invalid location '%s' for MVA %s — "
                            "must be a glass area code (e.g. WS, BW, FLD).",
                            i, location, mva,
                        )
                        sys.exit(1)
                    action = _resolve_row_work_item_action(row, default_action)
                else:
                    location = ""
                    action = ""

                targets.append({
                    "mva": mva,
                    "type": work_type,
                    "location": location,
                    "action": action,
                })

        log.info("[CREATE] Loaded %d MVA(s) from %s", len(targets), csv_path)
    else:
        targets.append({
            "mva": args.mva,
            "type": "Glass",
            "location": args.location or "WS",
            "action": args.action or "Replace",
        })

    return targets


async def process_mva(page, mva: str, complaint_type: str, location: str, action: str, step_delay_ms: int = 0) -> None:
    """Run the full work-item creation flow for a single MVA."""
    async def delay():
        if step_delay_ms:
            await page.wait_for_timeout(step_delay_ms)

    await pw_navigate_to_mva(page, mva)
    await delay()
    await check_existing_work_item(page, mva, complaint_type)
    await delay()
    await click_add_work_item(page, mva)
    await delay()
    await handle_complaint_dialog(page, mva, complaint_type, location, action, step_delay_ms)
    await delay()
    await complete_mileage_dialog(page, mva)
    await delay()
    await select_opcode(page, complaint_type)
    await delay()
    await create_work_item(page)
    await delay()
    await confirm_completion(page)


async def _run_playwright_creation_async(targets: list[dict]) -> None:
    """Create multiple work items in a single persistent session."""
    headless = resolve_headless()
    edge_user_data_dir = resolve_edge_user_data_dir()
    edge_profile_directory = resolve_edge_profile_directory()
    step_delay_ms = resolve_step_delay()

    log.info("[CREATE] %s", "=" * 50)
    log.info("[CREATE] Work item creation - %d MVA(s)", len(targets))
    log.info("[CREATE] Backend: playwright")
    log.info("[CREATE] Profile: %s", edge_profile_directory)
    log.info("[CREATE] %s", "=" * 50)

    created_count = 0
    skipped_count = 0
    failed_count = 0

    if _is_edge_running():
        log.warning("[CREATE] Edge processes detected — killing residual processes before launch...")
        subprocess.run(["taskkill", "/F", "/IM", "msedge.exe", "/T"], capture_output=True, text=True, check=False)
        time.sleep(2)
        if _is_edge_running():
            log.error("[CREATE] Edge is still running after kill attempt. Close all Edge windows and retry.")
            sys.exit(1)
        log.info("[CREATE] Edge processes cleared — proceeding with launch.")

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

            log.info("[CREATE] Warming up Compass with dummy MVA 50227203...")
            await pw_warmup_compass(page)
            log.info("[CREATE] Compass warm-up complete")

            for target in targets:
                mva = target["mva"]
                work_type = target["type"]
                location = target["location"]
                action = target["action"]

                try:
                    log.info("[CREATE] %s", "-" * 40)
                    log.info(
                        "[CREATE] MVA %s  |  type=%s  location=%s  action=%s",
                        mva, work_type, location or "—", action or "—",
                    )
                    try:
                        await process_mva(
                            page,
                            mva,
                            complaint_type=work_type,
                            location=location,
                            action=action,
                            step_delay_ms=step_delay_ms,
                        )
                    except ExistingWorkItemError as exc:
                        log.info("[CREATE] %s — Existing work item detected. Skipping. Reason: %s", mva, exc)
                        skipped_count += 1
                        continue

                    log.info("[CREATE] %s — Created", mva)
                    created_count += 1

                except Exception as e:
                    log.error("[CREATE] %s — FAILED: %s", mva, str(e))
                    failed_count += 1
                    continue

            await context.close()
            log.info("[CREATE] Browser closed.")

        except Exception as e:
            log.error("[CREATE] Session error: %s", str(e))
            await context.close()
            sys.exit(1)

    log.info("[CREATE] %s", "=" * 50)
    log.info("[CREATE] CREATION SUMMARY - %d MVA(s)", len(targets))
    log.info("[CREATE]   Created:  %d", created_count)
    log.info("[CREATE]   Skipped:  %d", skipped_count)
    log.info("[CREATE]   Failed:   %d", failed_count)
    log.info("[CREATE] %s", "=" * 50)

    if failed_count > 0 or (created_count == 0 and skipped_count == 0):
        sys.exit(1)


def _selenium_create(_targets: list[dict]) -> None:
    log.error("[CREATE] Selenium backend for batch create not yet implemented")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Create work items in batch with persistent session"
    )
    parser.add_argument("mva", nargs="?", help="Single MVA to create (omit if using --csv)")
    parser.add_argument("--csv", help="CSV file with columns: mva,Type,location,action")
    parser.add_argument("--location", default="WS", help="Location code for single-MVA mode (default: WS)")
    parser.add_argument("--action", choices=["Replace", "Repair"], help="Action for single-MVA Glass mode")
    parser.add_argument(
        "--backend",
        choices=["selenium", "playwright"],
        default="playwright",
        help="Browser backend — default: playwright",
    )

    args = parser.parse_args()

    if not args.mva and not args.csv:
        parser.error("Either provide MVA or use --csv")
    if args.mva and args.csv:
        parser.error("Cannot use both MVA and --csv together")

    targets = _build_create_targets(args)

    if not targets:
        log.error("[CREATE] No targets to process")
        sys.exit(1)

    if args.backend == "playwright":
        asyncio.run(_run_playwright_creation_async(targets))
    else:
        _selenium_create(targets)


if __name__ == "__main__":
    main()
