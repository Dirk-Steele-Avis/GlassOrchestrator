import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

from playwright_prototype.config import (
    resolve_browser_mode,
    resolve_debugger_address,
    resolve_edge_profile_directory,
    resolve_edge_user_data_dir,
    resolve_headless,
    resolve_initial_delay,
    resolve_step_delay,
)
from playwright_prototype.session import ensure_attached_context, ensure_profile_context
# DEPRECATED: this prototype is superseded by WorkItems/create_workitem.py
# Do not add new features here. Remove once WorkItems/ is confirmed stable.
from playwright_prototype.steps import (
    ExistingWorkItemError,
    check_existing_work_item,
    click_add_work_item,
    complete_mileage_dialog,
    confirm_completion,
    create_work_item,
    handle_complaint_dialog,
    navigate_to_mva,
    select_opcode,
    warmup_compass,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CSV = Path(__file__).resolve().parent / "sample_mvas.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "playwright_prototype.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("playwright_prototype")


async def _connect_attached_browser(pw: "Playwright", preferred_address: str) -> tuple["Browser", str]:
    """Attach to an already-running Edge CDP endpoint.

    Tries preferred address first, then common local debugger endpoints.
    """
    candidates = [
        preferred_address,
        "127.0.0.1:9222",
        "localhost:9222",
        "127.0.0.1:9223",
        "localhost:9223",
    ]

    tried: set[str] = set()
    last_error: Exception | None = None
    for address in candidates:
        normalized = address.strip()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        endpoint = f"http://{normalized}"
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint)
            return browser, normalized
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "Failed to attach to an open Edge debug session. "
        "Start Edge with --remote-debugging-port=9222 and your corporate profile, "
        f"then retry. Tried: {', '.join(tried)}. Last error: {last_error}"
    )


async def _launch_persistent_profile_context(
    pw: "Playwright",
    *,
    user_data_dir: Path,
    profile_directory: str,
    headless: bool,
) -> "BrowserContext":
    """Launch a new Playwright-controlled Edge window against a real profile."""
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        f"--profile-directory={profile_directory}",
    ]
    if not headless:
        launch_args.append("--start-maximized")

    try:
        return await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="msedge",
            headless=headless,
            chromium_sandbox=True,
            no_viewport=not headless,
            args=launch_args,
        )
    except Exception as exc:
        raise RuntimeError(
            "Failed to launch a persistent Edge profile session. "
            f"Close any running Edge windows using profile '{profile_directory}' and retry. "
            f"User data dir: {user_data_dir}. Error: {exc}"
        ) from exc


def load_csv(path: Path) -> list[dict]:
    """Load rows from a CSV file with columns: mva, location, action.

    location and action default to WS and Replace when blank.
    Rows with no mva value are skipped.
    """
    if not path.exists():
        log.error("CSV not found: %s", path)
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(line for line in f if not line.startswith("#"))
        for i, row in enumerate(reader, start=2):
            mva = (row.get("mva") or "").strip()
            if not mva:
                log.warning("Row %d: blank mva — skipping", i)
                continue
            rows.append({
                "mva": mva,
                "location": (row.get("location") or "WS").strip(),
                "action": (row.get("action") or "Replace").strip(),
            })
    return rows


async def process_mva(page, mva: str, location: str, action: str, step_delay_ms: int = 0) -> None:
    """Run the full work-item creation flow for a single MVA."""
    async def delay():
        if step_delay_ms:
            await page.wait_for_timeout(step_delay_ms)

    await navigate_to_mva(page, mva);                                        await delay()
    await check_existing_work_item(page, mva, "Glass");                      await delay()
    await click_add_work_item(page, mva);                                    await delay()
    await handle_complaint_dialog(page, mva, "Glass", location, action, step_delay_ms); await delay()
    await complete_mileage_dialog(page, mva);                                await delay()
    await select_opcode(page, "Glass");                                      await delay()
    await create_work_item(page);                                            await delay()
    await confirm_completion(page)


async def main(
    csv_path: Path = DEFAULT_CSV,
    pause: bool = False,
    browser_mode: str | None = None,
    debugger_address: str | None = None,
    edge_user_data_dir: Path | None = None,
    edge_profile_directory: str | None = None,
) -> int:
    headless = resolve_headless()
    resolved_browser_mode = browser_mode or resolve_browser_mode()
    resolved_debugger_address = debugger_address or resolve_debugger_address()
    resolved_edge_user_data_dir = edge_user_data_dir or resolve_edge_user_data_dir()
    resolved_edge_profile_directory = edge_profile_directory or resolve_edge_profile_directory()
    initial_delay_ms = resolve_initial_delay()
    step_delay_ms = resolve_step_delay()
    rows = load_csv(csv_path)
    if not rows:
        log.error("No rows loaded from %s — aborting", csv_path)
        return 1

    log.info(
        "Starting Playwright prototype — %d MVA(s) | mode=%s | headless=%s | initial_delay=%dms | step_delay=%dms | debugger=%s | user_data_dir=%s | profile=%s",
        len(rows),
        resolved_browser_mode,
        headless,
        initial_delay_ms,
        step_delay_ms,
        resolved_debugger_address,
        resolved_edge_user_data_dir,
        resolved_edge_profile_directory,
    )

    async with async_playwright() as pw:
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            if resolved_browser_mode == "attach":
                browser, attached_address = await _connect_attached_browser(
                    pw,
                    resolved_debugger_address,
                )
                log.info("Attached to Edge debug endpoint: %s", attached_address)
                context, page = await ensure_attached_context(browser, no_viewport=not headless)
            else:
                context = await _launch_persistent_profile_context(
                    pw,
                    user_data_dir=resolved_edge_user_data_dir,
                    profile_directory=resolved_edge_profile_directory,
                    headless=headless,
                )
                log.info(
                    "Launched persistent Edge profile session: %s (%s)",
                    resolved_edge_user_data_dir,
                    resolved_edge_profile_directory,
                )
                context, page = await ensure_profile_context(context)

            if initial_delay_ms > 0:
                log.info("[MAIN] Startup settle delay: %dms", initial_delay_ms)
                await page.wait_for_timeout(initial_delay_ms)

            await warmup_compass(page)

            if pause:
                row = rows[0]
                mva, location, action = row["mva"], row["location"], row["action"]
                log.info("[MAIN] --pause stepping through MVA %s", mva)
                visual_delay_ms = step_delay_ms if step_delay_ms >= 2000 else 2000

                async def step(coro, label: str):
                    if visual_delay_ms:
                        await page.wait_for_timeout(visual_delay_ms)
                    await coro
                    log.info("[MAIN] %s — pausing", label)
                    await page.pause()

                await step(navigate_to_mva(page, mva),                          "Step 1: navigated to MVA")
                await step(check_existing_work_item(page, mva, "Glass"),         "Step 2: pre-check no existing glass work item")
                await step(click_add_work_item(page, mva),                       "Step 3: Add Work Item clicked")
                await step(handle_complaint_dialog(page, mva, "Glass", location, action, step_delay_ms), "Step 4: complaint dialog handled")
                await step(complete_mileage_dialog(page, mva),                   "Step 5: mileage dialog advanced")
                await step(select_opcode(page, "Glass"),                         "Step 6: Glass opcode selected")
                await step(create_work_item(page),                               "Step 7: Create Work Item clicked")
                await step(confirm_completion(page),                             "Step 8: Done clicked — work item created")
                return 0

            summary = {"processed": 0, "created": 0, "skipped": 0, "failed": 0}
            for row in rows:
                mva = row["mva"]
                summary["processed"] += 1
                try:
                    await process_mva(page, mva, row["location"], row["action"], step_delay_ms)
                    summary["created"] += 1
                    log.info("[MAIN] %s — work item created", mva)
                except ExistingWorkItemError as exc:
                    summary["skipped"] += 1
                    log.warning("[MAIN] %s — skipped: %s", mva, exc)
                except Exception:
                    log.exception("[MAIN] %s — failed", mva)
                    summary["failed"] += 1

            log.info("Run complete — processed=%d created=%d skipped=%d failed=%d",
                     summary["processed"], summary["created"], summary["skipped"], summary["failed"])
            return 0 if summary["failed"] == 0 else 1

        except Exception:
            log.exception("Prototype failed during setup.")
            return 1
        finally:
            if context is not None:
                await context.close()
            elif browser is not None:
                await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Playwright prototype — Compass work item creation")
    parser.add_argument("--fresh", action="store_true", help="Delete storage_state.json before running to force a full login")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to MVA CSV file")
    parser.add_argument("--pause", action="store_true", help="Step through each action with Playwright Inspector pauses")
    parser.add_argument(
        "--browser-mode",
        choices=["profile", "attach"],
        default=None,
        help="Browser startup mode. Defaults to profile launch on this branch.",
    )
    parser.add_argument(
        "--debugger-address",
        type=str,
        default=None,
        help="Edge remote-debugging address (host:port). Defaults to 127.0.0.1:9222",
    )
    parser.add_argument(
        "--edge-user-data-dir",
        type=Path,
        default=None,
        help="Edge user data directory for persistent profile launches.",
    )
    parser.add_argument(
        "--edge-profile-directory",
        type=str,
        default=None,
        help="Edge profile directory name such as 'Default' or 'Profile 2'.",
    )
    args = parser.parse_args()

    if args.fresh:
        from playwright_prototype.config import STORAGE_STATE_PATH
        if STORAGE_STATE_PATH.exists():
            STORAGE_STATE_PATH.unlink()
            print(f"Deleted {STORAGE_STATE_PATH} — will perform full login")

    sys.exit(
        asyncio.run(
            main(
                csv_path=args.csv,
                pause=args.pause,
                browser_mode=args.browser_mode,
                debugger_address=args.debugger_address,
                edge_user_data_dir=args.edge_user_data_dir,
                edge_profile_directory=args.edge_profile_directory,
            )
        )
    )
