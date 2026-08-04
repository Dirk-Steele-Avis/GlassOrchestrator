"""
create_compass_complaints.py

Scaffold for Compass glass complaint batch creation.

V1 intent (per complaint.md):
- Read spreadsheet rows for today (Inventory Date only)
- Validate MVA
- For each row occurrence:
  - Check existing OPEN Glass Repair/Replace complaint
  - Skip if exists
  - Else create complaint + work item in one pass
- Continue on errors
- Append run log and print summary
- Support --dry-run

Current scaffold status:
- Spreadsheet read/filter/validation: implemented
- Compass lookup/create selectors: TODO (requires captured HTML / live verification)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import gspread  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "Missing dependency 'gspread'. Run with the project venv and install requirements."
    ) from exc

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_CONFIG_PATH = BASE_DIR / "orchestrator_config.json"
ORCHESTRATOR_PROJECT_CONFIG_PATH = BASE_DIR / "orchestrator_project.json"
ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_project.local.json"
ORCHESTRATOR_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_config.local.json"
SHARED_LOCAL_CONFIG_PATH = BASE_DIR / "config" / "config.local.json"

LOG_FILE = BASE_DIR / "CreateCompassComplaints.log"
COMPASS_HOME_URL = "https://avisbudget.palantirfoundry.com/workspace/module/view/latest/ri.workshop.main.module.d62ba12c-018c-41c1-8214-0749f6591b30"
COMPASS_VEHICLES_BUTTON_SELECTOR = "[data-test-id='workshop-inline-button']"
COMPASS_MVA_VIN_INPUT_SELECTOR = "[data-testid='mva-vin-input']"
COMPASS_MVA_VIN_SUBMIT_SELECTOR = "[data-testid='mva-vin-submit']"
COMPASS_MVA_VIN_INPUT_LABEL = "Or enter MVA/VIN"
COMPASS_SCAN_TAB_SELECTOR = 'button[role="tab"][data-key="scan"]'
COMPASS_MVA_INPUT_SELECTORS = [
    'input.bp6-input[placeholder*="Enter MVA"]',
    'input[type="text"][placeholder*="MVA"]',
    'div[role="tabpanel"][aria-hidden="false"] input[type="text"]',
    '[aria-label="Or enter MVA/VIN"]',
    COMPASS_MVA_VIN_INPUT_SELECTOR,
]
COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR = "input[type='search'][placeholder='Keyword Search (other fields)']"
COMPASS_WORKSHOP_OBJECT_TABLE_SELECTOR = "[data-test-id='workshop-object-table']"
COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR = "[data-test-id='workshop-object-title']"
COMPASS_DETAILS_PANEL_TABLE_SELECTOR = "[data-test-id='ov-full-object-view-tabs-content'] [data-test-id='workshop-object-table']"
COMPASS_WORK_ITEM_TILE_SELECTOR = "div[class*='fleet-operations-pwa__scan-record__']"
BROWSER_PROFILE_DIR = BASE_DIR / "outlook" / "browser_profile"
SETTLE_WAIT_MS = 15_000
GLASS_WORK_ITEM_PATTERN = re.compile(r"glass|windshield|crack|chip|window", re.I)

log = logging.getLogger("CreateCompassComplaints")


@dataclass
class CandidateRow:
    row_index: int
    mva: str
    inventory_date_raw: str


@dataclass
class RunSummary:
    total_rows_read: int = 0
    rows_for_day: int = 0
    skipped_invalid: int = 0
    skipped_existing: int = 0
    created: int = 0
    failed: int = 0
    dry_run_would_create: int = 0


@dataclass
class LookupResult:
    exists: bool
    reason: str
    table_text: str = ""
    title_texts: list[str] | None = None


# ------------------------------------------------------------
# Config and logging
# ------------------------------------------------------------


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _load_runtime_config() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in [
        ORCHESTRATOR_CONFIG_PATH,
        ORCHESTRATOR_PROJECT_CONFIG_PATH,
        ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH,
        ORCHESTRATOR_LOCAL_CONFIG_PATH,
        SHARED_LOCAL_CONFIG_PATH,
    ]:
        merged.update(_load_json(path))
    return merged


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def _parse_mva_override(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


# ------------------------------------------------------------
# Sheet helpers (mirrors FieldPOFillNextAction patterns)
# ------------------------------------------------------------


def _norm_header(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum())


def _find_col(headers: list[str], *names: str) -> int | None:
    normalized = {_norm_header(name) for name in names}
    for idx, header in enumerate(headers):
        if _norm_header(header) in normalized:
            return idx
    return None


def _parse_sheet_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_mva(raw: str) -> str:
    value = raw.strip()
    if len(value) == 8 and value.isdigit():
        return f"0{value}"
    return value


def _is_valid_mva(raw: str) -> bool:
    # Requirement: skip blank/invalid/non-numeric MVA rows.
    value = _normalize_mva(raw)
    return bool(value) and value.isdigit()


def _read_title_texts(table_locator) -> list[str]:
    titles = table_locator.locator(COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR)
    out: list[str] = []
    for idx in range(titles.count()):
        try:
            text = titles.nth(idx).inner_text(timeout=2000).strip()
        except Exception:
            text = ""
        if text:
            out.append(text)
    return out


def _extract_complaints_text(tile_text: str) -> str:
    for line in tile_text.splitlines():
        match = re.search(r"complaints\s*:\s*(.+)", line, re.I)
        if match:
            return match.group(1).strip()
    return ""


def _capture_work_item_debug_artifacts(page, mva: str, reason: str) -> None:
    debug_dir = BASE_DIR / "log" / "failures"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_mva = re.sub(r"[^0-9A-Za-z_-]", "_", mva)
    safe_reason = re.sub(r"[^0-9A-Za-z_-]", "_", reason)[:40]
    base = debug_dir / f"workitem_{safe_mva}_{safe_reason}_{stamp}"

    try:
        active_tab = page.get_by_role("tab", name=re.compile(r"active complaints", re.I)).first
        if active_tab.count() > 0:
            active_tab.click(timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass

    html_written = False
    selectors = [
        "table",
        "[role='table']",
        "[data-test-id='workshop-object-table']",
        COMPASS_DETAILS_PANEL_TABLE_SELECTOR,
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                html = locator.evaluate("el => el.outerHTML")
                (base.with_suffix(".html")).write_text(html, encoding="utf-8")
                html_written = True
                break
        except Exception:
            continue

    if not html_written:
        try:
            html = page.content()
            (base.with_suffix(".html")).write_text(html, encoding="utf-8")
        except Exception:
            pass

    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass

    log.info("Saved debug artifacts for %s failure to %s(.html/.png)", reason, base)


def _find_glass_complaint_row(page):
    row_selector = "tr, [role='row']"
    table_selector = "table, [role='table']"

    tables = page.locator(table_selector)
    for i in range(tables.count()):
        table = tables.nth(i)
        try:
            text = (table.inner_text(timeout=1500) or "").lower()
        except Exception:
            continue

        # Active Complaints grid includes these headers; use them to avoid matching unrelated tables.
        if "attached work items" not in text or "title" not in text:
            continue

        rows = table.locator(row_selector).filter(has_text=re.compile(r"glass", re.I))
        for r in range(rows.count()):
            row = rows.nth(r)
            try:
                row_text = (row.inner_text(timeout=1000) or "").lower()
            except Exception:
                continue
            if "glass" in row_text:
                return table, row

    return None, None


def _attached_work_items_count(table, row) -> int | None:
    headers = table.locator("thead th, [role='columnheader']")
    attached_idx: int | None = None
    for i in range(headers.count()):
        try:
            header_text = (headers.nth(i).inner_text(timeout=1000) or "").strip().lower()
        except Exception:
            continue
        if "attached" in header_text and "work" in header_text and "item" in header_text:
            attached_idx = i
            break

    if attached_idx is None:
        return None

    cells = row.locator("td, [role='cell']")
    if cells.count() <= attached_idx:
        return None

    try:
        raw = (cells.nth(attached_idx).inner_text(timeout=1000) or "").strip()
    except Exception:
        return None

    match = re.search(r"\d+", raw)
    if not match:
        return None
    return int(match.group(0))


def _find_glass_row_index_blueprint(page) -> int | None:
    # Blueprint table is div-based (bp6-table-cell-row-X / col-Y), not tr/td.
    title_cells = page.locator("div[class*='bp6-table-cell-row-'][class*='bp6-table-cell-col-1']")
    for i in range(title_cells.count()):
        cell = title_cells.nth(i)
        try:
            text = (cell.inner_text(timeout=1000) or "").strip().lower()
        except Exception:
            continue
        if "glass damage" not in text:
            continue

        class_attr = cell.get_attribute("class") or ""
        match = re.search(r"bp6-table-cell-row-(\d+)", class_attr)
        if match:
            return int(match.group(1))
    return None


def _attached_work_items_count_blueprint(page, row_idx: int) -> int | None:
    # In the shared HTML, Attached Work Items is rendered in col-4.
    cell = page.locator(
        f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-4']"
    ).first
    try:
        if cell.count() == 0:
            return None
        raw = (cell.inner_text(timeout=1000) or "").strip()
    except Exception:
        return None

    match = re.search(r"\d+", raw)
    if not match:
        return None
    return int(match.group(0))


def _select_glass_complaint_row_blueprint(page, row_idx: int) -> bool:
    # First try the explicit Blueprint row checkbox input and force a checked state.
    row_selector = f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-0']"
    explicit = page.locator(
        f"{row_selector} input[aria-label='Select row'][type='checkbox']"
    ).first
    try:
        if explicit.count() > 0 and explicit.is_visible():
            if not explicit.is_checked():
                explicit.check(force=True, timeout=5000)
                page.wait_for_timeout(150)
            if not explicit.is_checked():
                explicit.click(force=True, timeout=5000)
            if explicit.is_checked():
                return True
    except Exception:
        pass

    # Some Blueprint renders intercept clicks; target label/indicator then re-check input state.
    indicator = page.locator(f"{row_selector} label.bp6-checkbox .bp6-control-indicator").first
    try:
        if explicit.count() > 0 and indicator.count() > 0 and indicator.is_visible():
            indicator.click(force=True, timeout=5000)
            page.wait_for_timeout(150)
            if explicit.is_checked():
                return True
    except Exception:
        pass

    # Prefer explicit checkbox-like controls in the left row selector area.
    candidates = [
        page.locator(
            f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-0'] input[type='checkbox']"
        ).first,
        page.locator(
            f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-0'] [role='checkbox']"
        ).first,
        page.locator(
            f"div[class*='bp6-table-row-header-cell'][class*='bp6-table-cell-row-{row_idx}'] [role='checkbox']"
        ).first,
        page.locator(
            f"div[class*='bp6-table-row-header-cell'][class*='bp6-table-cell-row-{row_idx}'] .bp6-control-indicator"
        ).first,
        page.locator(f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-0']").first,
        page.locator(f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-1']").first,
    ]
    for candidate in candidates:
        try:
            if candidate.count() == 0:
                continue
            candidate.wait_for(state="visible", timeout=3000)
            candidate.click(timeout=5000)
            page.wait_for_timeout(300)
            try:
                if explicit.count() > 0 and explicit.is_checked():
                    return True
            except Exception:
                continue
        except Exception:
            continue
    return False


def _wait_for_work_item_dialog(page) -> tuple[bool, Any]:
    # Dialog is considered visible if Create Work Item heading or Op Code field appears.
    markers = [
        page.get_by_role("heading", name=re.compile(r"create work item", re.I)).first,
        page.get_by_role("button", name=re.compile(r"create work item", re.I)).first,
        page.get_by_text(re.compile(r"create work item", re.I)).first,
        page.get_by_text(re.compile(r"op\s*code|opcode", re.I)).first,
    ]
    for _ in range(20):
        for marker in markers:
            try:
                if marker.count() > 0 and marker.is_visible():
                    try:
                        heading = page.get_by_role("heading", name=re.compile(r"create work item", re.I)).first
                        if heading.count() > 0 and heading.is_visible():
                            return True, heading.locator("xpath=ancestor::div[contains(@class,'workshop-section')][1]")
                    except Exception:
                        pass

                    try:
                        create_btn = page.get_by_role("button", name=re.compile(r"create work item", re.I)).first
                        if create_btn.count() > 0 and create_btn.is_visible():
                            return True, create_btn.locator("xpath=ancestor::div[contains(@class,'workshop-section')][1]")
                    except Exception:
                        pass

                    return True, page
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False, page


def _is_add_work_item_selected_enabled(page) -> bool:
    try:
        btn = page.get_by_role(
            "button",
            name=re.compile(r"add work item to selected complaints", re.I),
        ).first
        btn.wait_for(state="visible", timeout=7000)
        handle = btn.element_handle()
        if handle is None:
            return False
        state = page.evaluate(
            """
            el => ({
                disabled: el.hasAttribute('disabled'),
                aria: el.getAttribute('aria-disabled'),
                className: (el.className || '').toString()
            })
            """,
            handle,
        )
        class_name = (state.get("className") or "").lower()
        return (not state.get("disabled")) and state.get("aria") != "true" and "disabled" not in class_name
    except Exception:
        return False


def _is_button_enabled(locator) -> bool:
    try:
        handle = locator.element_handle()
        if handle is None:
            return False
        page = locator.page
        state = page.evaluate(
            """
            el => ({
                disabled: el.hasAttribute('disabled'),
                aria: el.getAttribute('aria-disabled'),
                className: (el.className || '').toString()
            })
            """,
            handle,
        )
        class_name = (state.get("className") or "").lower()
        return (not state.get("disabled")) and state.get("aria") != "true" and "disabled" not in class_name
    except Exception:
        return False


def _has_open_glass_work_item(page) -> tuple[bool, str]:
    try:
        try:
            page.get_by_role("tab", name=re.compile(r"active complaints", re.I)).first.click(timeout=4000)
            page.wait_for_timeout(300)
        except Exception:
            pass

        row_idx = _find_glass_row_index_blueprint(page)
        if row_idx is not None:
            attached_count = _attached_work_items_count_blueprint(page, row_idx)
            if attached_count is not None and attached_count > 0:
                return True, f"attached_work_items={attached_count}"

        table, row = _find_glass_complaint_row(page)
        if row is not None and table is not None:
            attached_count = _attached_work_items_count(table, row)
            if attached_count is not None and attached_count > 0:
                return True, f"attached_work_items={attached_count}"

        tiles = page.locator(COMPASS_WORK_ITEM_TILE_SELECTOR)
        count = tiles.count()
        if count == 0:
            return False, "no_work_item_tiles"

        for idx in range(count):
            text = (tiles.nth(idx).inner_text(timeout=3000) or "").strip()
            if not text:
                continue
            if not re.search(r"\bopen\b", text, re.I):
                continue
            complaints = _extract_complaints_text(text)
            if complaints and GLASS_WORK_ITEM_PATTERN.search(complaints):
                return True, "open_glass_work_item_found"

        return False, "open_glass_work_item_not_present"
    except Exception as exc:
        return False, f"work_item_check_failed: {exc}"


def _open_home_page(page) -> None:
    page.goto(COMPASS_HOME_URL)
    page.wait_for_load_state("domcontentloaded")
    log.info("Compass page after goto: url=%s title=%s", page.url, page.title())
    page.wait_for_timeout(SETTLE_WAIT_MS)


def _click_vehicles(page):
    candidate_page = page

    vehicles_button = page.locator(COMPASS_VEHICLES_BUTTON_SELECTOR).filter(has_text="Vehicles").first
    try:
        with page.expect_popup(timeout=10_000) as popup_info:
            vehicles_button.click()
        candidate_page = popup_info.value
        candidate_page.wait_for_load_state("domcontentloaded")
        log.info("Compass Vehicles click opened a popup/tab: %s", candidate_page.url)
    except Exception:
        vehicles_button.click()
    candidate_page.wait_for_timeout(700)
    try:
        body_text = candidate_page.locator("body").inner_text(timeout=3000)
        log.info("Compass page after Vehicles click: %s", body_text[:800].replace("\n", " | "))
    except Exception:
        log.info("Compass page after Vehicles click: body text unavailable")
    vehicle_search_tile = candidate_page.get_by_text("vehicle search", exact=False)
    try:
        if vehicle_search_tile.count() > 0 and vehicle_search_tile.first.is_visible():
            with candidate_page.expect_popup(timeout=10_000) as popup_info:
                vehicle_search_tile.first.click()
            candidate_page = popup_info.value
            candidate_page.wait_for_load_state("domcontentloaded")
            log.info("Compass vehicle search click opened a popup/tab: %s", candidate_page.url)
            candidate_page.wait_for_timeout(1200)
    except Exception:
        pass
    candidate_page.wait_for_timeout(SETTLE_WAIT_MS)
    try:
        popup_body_text = candidate_page.locator("body").inner_text(timeout=3000)
        log.info("Compass popup body: %s", popup_body_text[:1200].replace("\n", " | "))
    except Exception:
        log.info("Compass popup body: unavailable")
    scan_tab_candidates = [
        candidate_page.locator(COMPASS_SCAN_TAB_SELECTOR).first,
        candidate_page.get_by_role("tab", name="Scan", exact=True).first,
        candidate_page.get_by_role("button", name="Scan", exact=True).first,
    ]
    for scan_tab in scan_tab_candidates:
        try:
            if scan_tab.is_visible():
                scan_tab.click()
                candidate_page.wait_for_timeout(700)
                break
        except Exception:
            continue
    return candidate_page


def _wait_for_mva_input(page, timeout_s: int = 45):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for selector in COMPASS_MVA_INPUT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.is_visible():
                    return locator
            except Exception:
                continue
        page.wait_for_timeout(1000)

    raise TimeoutError(f"MVA/VIN input did not become visible within {timeout_s}s")


def _wait_for_keyword_search_input(page, timeout_s: int = 20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            locator = page.locator(COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR).first
            if locator.is_visible():
                return locator
        except Exception:
            pass
        page.wait_for_timeout(1000)
    raise TimeoutError(f"Keyword search input did not become visible within {timeout_s}s")


def _search_mva(page, mva: str) -> None:
    try:
        search = _wait_for_mva_input(page, timeout_s=10)
        search.click()
        search.fill(mva)
        try:
            page.get_by_role("button", name="Enter", exact=True).first.click()
        except Exception:
            search.press("Enter")
        page.wait_for_timeout(1200)
        page.wait_for_timeout(SETTLE_WAIT_MS)
        return
    except Exception:
        log.info("MVA/VIN input path unavailable, falling back to keyword search input")

    keyword = _wait_for_keyword_search_input(page)
    keyword.click()
    keyword.fill(mva)
    keyword.press("Enter")
    page.wait_for_timeout(1500)

    # Vehicle search list typically renders the MVA text in the left panel.
    # Clicking the matching vehicle row is read-only navigation.
    row = page.get_by_text(mva, exact=False)
    try:
        if row.count() > 0 and row.first.is_visible():
            row.first.click()
    except Exception:
        pass

    page.wait_for_timeout(SETTLE_WAIT_MS)


def _inspect_glass_complaint(page, mva: str) -> LookupResult:
    table = page.locator(COMPASS_DETAILS_PANEL_TABLE_SELECTOR).first
    try:
        table.wait_for(state="visible", timeout=4000)
    except PlaywrightTimeoutError:
        return LookupResult(False, "complaint_table_not_visible")

    try:
        table_text = table.inner_text(timeout=3000)
    except Exception:
        table_text = ""

    title_texts = _read_title_texts(table)
    if any(text.strip().lower() == "glass damage" for text in title_texts):
        return LookupResult(True, "glass_damage_found", table_text=table_text, title_texts=title_texts)

    if "glass damage" in table_text.lower():
        return LookupResult(True, "glass_damage_found_in_table_text", table_text=table_text, title_texts=title_texts)

    return LookupResult(False, "glass_damage_not_present", table_text=table_text, title_texts=title_texts)


def _click_button_by_name(page, pattern: str, timeout_ms: int = 10_000) -> None:
    page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=timeout_ms)


def _complaint_popup_scope(page):
    # Scope to the active complaint popup when present.
    heading = page.get_by_role("heading", name=re.compile(r"create complaint for mva", re.I)).first
    try:
        heading.wait_for(state="visible", timeout=8_000)
        return heading.locator("xpath=ancestor::div[contains(@class,'workshop-section')][1]")
    except Exception:
        return page


def _set_yes_drivable(scope) -> None:
    # Click the visible label text first so UI state updates in React.
    yes_label = scope.locator("label", has_text=re.compile(r"^\s*Yes\s*$", re.I)).first
    yes_label.wait_for(state="visible", timeout=10_000)
    yes_label.click(timeout=10_000)

    yes_radio = scope.locator("input[type='radio'][value='Yes']").first
    yes_radio.wait_for(state="attached", timeout=10_000)
    if not yes_radio.is_checked():
        yes_radio.check(force=True)
    if not yes_radio.is_checked():
        raise RuntimeError("Could not set Is Vehicle Drivable? to Yes")


def _set_glass_damage_category(scope) -> None:
    category_label = scope.locator("label", has_text=re.compile(r"^\s*Glass Damage\s*$", re.I)).first
    category_label.wait_for(state="visible", timeout=10_000)
    category_label.click(timeout=10_000)

    category = scope.locator("input[type='radio'][value='Glass Damage']").first
    category.wait_for(state="attached", timeout=10_000)
    if not category.is_checked():
        category.check(force=True)
    if not category.is_checked():
        raise RuntimeError("Could not set Category to Glass Damage")


def _fill_complaint_description(scope, text: str) -> None:
    # Confirmed popup uses a textarea under Complaint Description.
    candidates = [
        scope.locator("textarea.bp6-text-area").first,
        scope.locator("textarea").first,
        scope.locator('input[placeholder*="Complaint" i]').first,
    ]
    for candidate in candidates:
        try:
            if candidate.is_visible():
                candidate.fill(text, timeout=8_000)
                current = candidate.input_value()
                if current.strip() != text:
                    candidate.click(timeout=5_000)
                    candidate.press("Control+a")
                    candidate.type(text, delay=20)
                    current = candidate.input_value()
                if current.strip() == text:
                    return
                return
        except Exception:
            continue

    raise RuntimeError("Complaint Description input not found")


def _select_subcategory_if_present(scope) -> bool:
    # Some flows require a sub-category before Submit Complaint is enabled.
    options = [
        r"windshield\s*crack",
        r"side/rear\s*window\s*damage",
        r"windshield\s*chip",
    ]
    for pattern in options:
        try:
            option = scope.locator("label", has_text=re.compile(pattern, re.I)).first
            if option.count() > 0 and option.is_visible():
                option.click(timeout=8_000)
                return True
        except Exception:
            continue
    return False


def _create_complaint_only(page, mva: str) -> tuple[bool, str]:
    try:
        _click_button_by_name(page, r"create\s*complaint")
        page.wait_for_timeout(1200)
        scope = _complaint_popup_scope(page)

        _set_yes_drivable(scope)
        page.wait_for_timeout(600)

        _set_glass_damage_category(scope)
        page.wait_for_timeout(600)

        _fill_complaint_description(scope, "Glass Damage")
        page.wait_for_timeout(500)

        # Confirmed markup is an anchor role=button that starts disabled.
        submit = scope.locator("a[role='button']", has_text="Submit Complaint").first
        submit.wait_for(state="visible", timeout=10_000)
        log.info(
            "Complaint form state before submit: drivable_yes=%s category_glass=%s",
            scope.locator("input[type='radio'][value='Yes']").first.is_checked(),
            scope.locator("input[type='radio'][value='Glass Damage']").first.is_checked(),
        )
        submit_el = submit.element_handle()
        if submit_el is None:
            raise RuntimeError("Submit Complaint button handle unavailable")

        def _submit_ready() -> bool:
            state = page.evaluate(
                "el => ({ aria: el.getAttribute('aria-disabled'), disabled: el.hasAttribute('disabled') })",
                submit_el,
            )
            return state.get("aria") != "true" and not state.get("disabled")

        if not _submit_ready():
            used_subcategory = _select_subcategory_if_present(scope)
            if used_subcategory:
                page.wait_for_timeout(1000)

        page.wait_for_function(
            "el => el && el.getAttribute('aria-disabled') !== 'true' && !el.hasAttribute('disabled')",
            arg=submit_el,
            timeout=15_000,
        )
        submit.click(timeout=10_000)
        page.wait_for_timeout(SETTLE_WAIT_MS)

        verify = _inspect_glass_complaint(page, mva)
        if verify.exists:
            return True, "complaint_created"
        return False, "submitted_but_not_visible"
    except Exception as exc:
        return False, f"complaint_create_failed: {exc}"


def _create_work_item_for_glass_complaint(page, mva: str) -> tuple[bool, str]:
    try:
        # Ensure Active Complaints table is in view before selecting a complaint row.
        try:
            page.get_by_role("tab", name=re.compile(r"active complaints", re.I)).first.click(timeout=5000)
        except Exception:
            pass

        row_idx = _find_glass_row_index_blueprint(page)
        checked = False
        if row_idx is not None:
            checked = _select_glass_complaint_row_blueprint(page, row_idx)
            if checked:
                # Verify the row selection by ensuring the add-to-selected button is enabled.
                if not _is_add_work_item_selected_enabled(page):
                    # Retry once in case first click hit only row focus.
                    checked = _select_glass_complaint_row_blueprint(page, row_idx)
                if not _is_add_work_item_selected_enabled(page):
                    checked = False
        else:
            table, complaint_row = _find_glass_complaint_row(page)
            if complaint_row is None or table is None:
                _capture_work_item_debug_artifacts(page, mva, "glass_row_missing")
                return False, "glass_damage_row_not_found_in_active_complaints"

            row_checkbox = complaint_row.locator("input[type='checkbox'], [role='checkbox']").first
            try:
                row_checkbox.wait_for(state="visible", timeout=5000)
                row_checkbox.click(timeout=5000)
                checked = True
            except Exception:
                try:
                    complaint_row.click(timeout=5000)
                    page.wait_for_timeout(300)
                    checked = True
                except Exception:
                    checked = False

            if checked and not _is_add_work_item_selected_enabled(page):
                checked = False

        if not checked:
            _capture_work_item_debug_artifacts(page, mva, "glass_checkbox_not_selected")
            return False, "failed_to_select_glass_complaint_checkbox"

        add_button = page.get_by_role(
            "button",
            name=re.compile(r"add work item to selected complaints", re.I),
        ).first
        add_button.wait_for(state="visible", timeout=10_000)
        add_button.click(timeout=10_000)
        page.wait_for_timeout(1200)

        dialog_visible, scope = _wait_for_work_item_dialog(page)
        if not dialog_visible:
            _capture_work_item_debug_artifacts(page, mva, "workitem_dialog_not_visible")
            return False, "work_item_dialog_not_visible"

        # Scope to the Add Work Item modal to avoid matching top-level search controls.
        modal_scope = page
        try:
            modal_title = page.get_by_text(re.compile(r"add work item to complaints", re.I)).first
            if modal_title.count() > 0 and modal_title.is_visible():
                modal_scope = modal_title.locator(
                    "xpath=ancestor::*[contains(@class,'bp6-dialog') or contains(@class,'workshop-section')][1]"
                )
        except Exception:
            modal_scope = page

        scope = modal_scope

        dropdown_opened = False
        dropdown_candidates = [
            scope.locator("button", has_text=re.compile(r"select an option\.\.\.", re.I)).first,
            scope.locator("[class*='bp6-button-text']", has_text=re.compile(r"select an option\.\.\.", re.I)).first,
            scope.get_by_text(re.compile(r"^select an option\.\.\.$", re.I)).first,
            scope.locator("[class*='bp6-select'] [class*='bp6-button-text']", has_text=re.compile(r"select an option", re.I)).first,
            scope.locator("[class*='bp6-popover-target']", has_text=re.compile(r"select an option", re.I)).first,
            scope.locator("button[aria-haspopup='listbox'], button[aria-haspopup='menu']").first,
            scope.get_by_role("combobox", name=re.compile(r"op\s*code|opcode", re.I)).first,
            scope.locator("[role='combobox']").filter(has_text=re.compile(r"op\s*code|opcode", re.I)).first,
            scope.get_by_label(re.compile(r"op\s*code|opcode", re.I)).first,
            scope.locator("label", has_text=re.compile(r"op\s*code|opcode", re.I)).first,
            scope.locator("button", has_text=re.compile(r"op\s*code|opcode", re.I)).first,
            scope.get_by_text(re.compile(r"op\s*code|opcode", re.I)).first,
        ]
        for candidate in dropdown_candidates:
            try:
                candidate.wait_for(state="visible", timeout=3000)
                candidate.click(timeout=5000)
                dropdown_opened = True
                break
            except Exception:
                continue

        if not dropdown_opened:
            _capture_work_item_debug_artifacts(page, mva, "opcode_dropdown_missing")
            return False, "opcode_dropdown_not_found"

        opcode_selected = False
        selected_opcode_text = ""
        exact_opcode_pattern = re.compile(r"^glass\s*repair\s*/\s*replace$", re.I)

        try:
            popup_search = page.locator(
                ".bp6-portal input[placeholder*='Search' i]:visible, [role='listbox'] input[placeholder*='Search' i]:visible, [role='menu'] input[placeholder*='Search' i]:visible"
            ).last
            if popup_search.count() == 0:
                popup_search = scope.locator("input[placeholder*='Search' i]:visible").last
            popup_search.wait_for(state="visible", timeout=4000)
            popup_search.click(timeout=4000)
            popup_search.fill("", timeout=4000)
            popup_search.press("Control+a")
            popup_search.type("glass", delay=75)
            typed_value = (popup_search.input_value(timeout=2000) or "").strip().lower()
            if typed_value != "glass":
                popup_search.fill("glass", timeout=4000)
                typed_value = (popup_search.input_value(timeout=2000) or "").strip().lower()
            if typed_value != "glass":
                return False, f"opcode_search_input_not_set: {typed_value or 'empty'}"
            page.wait_for_timeout(700)

            option_candidates = [
                page.locator("[role='listbox'] [role='option']").first,
                page.locator("[role='menu'] [role='menuitem']").first,
                scope.locator("[class*='opCodeText']").first,
            ]
            top_option = None
            for candidate in option_candidates:
                try:
                    candidate.wait_for(state="visible", timeout=4000)
                    top_option = candidate
                    break
                except Exception:
                    continue

            if top_option is not None:
                selected_opcode_text = (top_option.inner_text(timeout=1000) or "").strip()
                if exact_opcode_pattern.match(selected_opcode_text):
                    top_option.click(timeout=6000)
                    opcode_selected = True
                else:
                    return False, f"unexpected_top_opcode_option: {selected_opcode_text or 'unknown'}"
        except Exception:
            opcode_selected = False

        if not opcode_selected:
            _capture_work_item_debug_artifacts(page, mva, "opcode_option_missing")
            return False, "glass_opcode_not_found"

        page.wait_for_timeout(700)

        selected_value_confirmed = False
        selected_value_candidates = [
            scope.locator("button", has_text=exact_opcode_pattern).first,
            scope.locator("[class*='bp6-button-text']", has_text=exact_opcode_pattern).first,
            scope.get_by_text(exact_opcode_pattern).first,
        ]
        for candidate in selected_value_candidates:
            try:
                if candidate.count() > 0 and candidate.is_visible():
                    selected_value_confirmed = True
                    break
            except Exception:
                continue

        if not selected_value_confirmed:
            _capture_work_item_debug_artifacts(page, mva, "opcode_value_not_confirmed")
            return False, f"glass_opcode_not_confirmed: {selected_opcode_text or 'unknown'}"

        create_button = scope.get_by_role("button", name=re.compile(r"create work item", re.I)).first
        create_button.wait_for(state="visible", timeout=15_000)
        if not _is_button_enabled(create_button):
            page.wait_for_timeout(1200)
        if not _is_button_enabled(create_button):
            _capture_work_item_debug_artifacts(page, mva, "create_work_item_disabled")
            return False, "create_work_item_disabled"

        create_button.click(timeout=10_000)

        done_clicked = False
        try:
            done_button = page.get_by_role("button", name=re.compile(r"^done$", re.I)).first
            done_button.wait_for(state="visible", timeout=12_000)
            done_button.click(timeout=10_000)
            done_clicked = True
        except Exception:
            done_clicked = False

        page.wait_for_timeout(1200)

        exists, reason = _has_open_glass_work_item(page)
        if exists:
            if done_clicked:
                return True, "work_item_created_done_clicked"
            return True, "work_item_created_done_not_required"
        return False, f"work_item_not_visible_after_done: {reason}"
    except Exception as exc:
        return False, f"work_item_create_failed: {exc}"


def _collect_candidates(values: list[list[str]], run_day: date) -> tuple[list[CandidateRow], RunSummary]:
    if not values:
        raise RuntimeError("Sheet is empty.")

    headers = values[0]
    inv_col = _find_col(headers, "Inventory Date")
    mva_col = _find_col(headers, "MVA")

    missing = []
    if inv_col is None:
        missing.append("Inventory Date")
    if mva_col is None:
        missing.append("MVA")
    if missing:
        raise RuntimeError(f"Required column(s) missing: {', '.join(missing)}")

    summary = RunSummary(total_rows_read=max(0, len(values) - 1))
    candidates: list[CandidateRow] = []

    for row_idx, row in enumerate(values[1:], start=2):
        inv_raw = row[inv_col].strip() if len(row) > inv_col else ""
        inv_date = _parse_sheet_date(inv_raw)
        if inv_date != run_day:
            continue

        summary.rows_for_day += 1

        mva_raw = row[mva_col].strip() if len(row) > mva_col else ""
        if not inv_raw or inv_date is None or not _is_valid_mva(mva_raw):
            summary.skipped_invalid += 1
            log.info(
                "Row %d skipped invalid data: Inventory Date='%s', MVA='%s'",
                row_idx,
                inv_raw,
                mva_raw,
            )
            continue

        candidates.append(
            CandidateRow(
                row_index=row_idx,
                mva=_normalize_mva(mva_raw),
                inventory_date_raw=inv_raw,
            )
        )

    return candidates, summary


def _build_single_candidate(raw_mva: str) -> tuple[list[CandidateRow], RunSummary]:
    mva = _normalize_mva(raw_mva)
    if not _is_valid_mva(mva):
        raise RuntimeError(f"Invalid MVA override: {raw_mva!r}")
    summary = RunSummary(total_rows_read=1, rows_for_day=1)
    return [CandidateRow(row_index=1, mva=mva, inventory_date_raw=str(date.today()))], summary


# ------------------------------------------------------------
# Compass flow scaffold
# ------------------------------------------------------------


def _check_existing_open_glass_complaint(mva: str) -> tuple[bool, str]:
    """
    Thin prototype lookup: open HomePage, search for the MVA, and confirm whether
    an Active Complaints row titled exactly 'Glass Damage' is present.

    Read-only only:
    - no complaint-row selection
    - no row clicking
    - no create actions

    Returns:
    - (True, reason) when existing OPEN matching complaint found
    - (False, reason) when no matching complaint found
    """
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.new_page()
            _open_home_page(page)
            page = _click_vehicles(page)
            _search_mva(page, mva)
            result = _inspect_glass_complaint(page, mva)

            if result.exists:
                log.info("MVA %s -> glass complaint exists (%s)", mva, result.reason)
                return True, result.reason

            log.info("MVA %s -> no glass complaint row present (%s)", mva, result.reason)
            return False, result.reason
        except Exception as exc:
            log.error("MVA %s -> lookup failed: %s", mva, exc)
            return False, f"lookup_failed: {exc}"
        finally:
            context.close()


def _create_glass_complaint_and_work_item(mva: str) -> tuple[bool, str]:
    """
    Create a Glass Damage complaint when none exists.

    Steps:
    1) Click Create Complaint
    2) Is Vehicle Drivable? -> Yes
    3) Category -> Glass Damage
    4) Complaint Description -> Glass Damage
    5) Click Submit Complaint

    Returns:
    - (True, reason) on success
    - (False, reason) on failure
    """
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.new_page()
            _open_home_page(page)
            page = _click_vehicles(page)
            _search_mva(page, mva)
            return _create_complaint_only(page, mva)
        except Exception as exc:
            return False, f"create_flow_failed: {exc}"
        finally:
            context.close()


def _process_candidates(candidates: list[CandidateRow], dry_run: bool, summary: RunSummary) -> RunSummary:
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.new_page()
            _open_home_page(page)
            page = _click_vehicles(page)
            log.info("Compass processing session initialized once; reusing keyword search between MVAs")

            for item in candidates:
                try:
                    # Keep processing in the same Vehicle Search context between MVAs.
                    try:
                        _wait_for_keyword_search_input(page, timeout_s=6)
                    except Exception:
                        log.info("MVA %s -> keyword search not visible; reinitializing Vehicles context", item.mva)
                        _open_home_page(page)
                        page = _click_vehicles(page)

                    _search_mva(page, item.mva)
                    lookup = _inspect_glass_complaint(page, item.mva)
                    has_work_item, work_item_reason = _has_open_glass_work_item(page)

                    if lookup.exists and has_work_item:
                        summary.skipped_existing += 1
                        log.info(
                            "Row %d MVA %s -> skipped existing (%s, %s)",
                            item.row_index,
                            item.mva,
                            lookup.reason,
                            work_item_reason,
                        )
                        continue

                    if lookup.exists and not has_work_item:
                        log.info("MVA %s -> glass complaint exists but work item missing (%s)", item.mva, work_item_reason)
                    else:
                        log.info("MVA %s -> no glass complaint row present (%s)", item.mva, lookup.reason)

                    if dry_run:
                        summary.dry_run_would_create += 1
                        if lookup.exists:
                            log.info(
                                "DRY-RUN Row %d MVA %s -> would create work item (%s)",
                                item.row_index,
                                item.mva,
                                work_item_reason,
                            )
                        else:
                            log.info(
                                "DRY-RUN Row %d MVA %s -> would create complaint + work item (%s)",
                                item.row_index,
                                item.mva,
                                lookup.reason,
                            )
                        continue

                    if not lookup.exists:
                        complaint_created, complaint_reason = _create_complaint_only(page, item.mva)
                        if not complaint_created:
                            summary.failed += 1
                            log.error(
                                "Row %d MVA %s -> failed create complaint (%s)",
                                item.row_index,
                                item.mva,
                                complaint_reason,
                            )
                            continue

                    work_item_created, work_item_create_reason = _create_work_item_for_glass_complaint(page, item.mva)
                    if work_item_created:
                        summary.created += 1
                        log.info("Row %d MVA %s -> work item created", item.row_index, item.mva)
                    else:
                        summary.failed += 1
                        log.error(
                            "Row %d MVA %s -> failed create work item (%s)",
                            item.row_index,
                            item.mva,
                            work_item_create_reason,
                        )

                except Exception as exc:
                    summary.failed += 1
                    log.error("Row %d MVA %s -> exception: %s", item.row_index, item.mva, exc)
        finally:
            context.close()

    return summary


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Compass complaints from today's MVAs")
    parser.add_argument(
        "--mva",
        help="Prototype override: process a single MVA directly instead of reading the sheet.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks and logging only; do not create complaints/work items.",
    )
    return parser.parse_args()


def run() -> None:
    _setup_logging()
    args = _parse_args()

    run_mode = "DRY-RUN" if args.dry_run else "LIVE"
    log.info("Starting CreateCompassComplaints (%s)", run_mode)
    log.info("Initial Compass page: %s", COMPASS_HOME_URL)

    if args.mva:
        candidates, summary = _build_single_candidate(args.mva)
        log.info("Prototype MVA override enabled: %s", candidates[0].mva)
    else:
        config = _load_runtime_config()
        spreadsheet_id = str(config.get("spreadsheet_id", "")).strip()
        sheet_name = str(config.get("sheet_name", "GlassClaims")).strip() or "GlassClaims"
        service_account_path = _resolve_path(str(config.get("service_account_json", "Service_account.json")))

        if not spreadsheet_id:
            raise RuntimeError("Missing spreadsheet_id in orchestrator config.")
        if not service_account_path.exists():
            raise RuntimeError(f"Service account file not found: {service_account_path}")

        gc = gspread.service_account(filename=str(service_account_path))
        sh = gc.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)

        values = ws.get_all_values()
        candidates, summary = _collect_candidates(values, run_day=date.today())

    log.info(
        "Rows read=%d, rows_for_day=%d, candidates=%d, skipped_invalid=%d",
        summary.total_rows_read,
        summary.rows_for_day,
        len(candidates),
        summary.skipped_invalid,
    )

    summary = _process_candidates(candidates, args.dry_run, summary)

    log.info(
        "Complete. total_rows_read=%d, rows_for_day=%d, skipped_invalid=%d, "
        "skipped_existing=%d, created=%d, failed=%d, dry_run_would_create=%d",
        summary.total_rows_read,
        summary.rows_for_day,
        summary.skipped_invalid,
        summary.skipped_existing,
        summary.created,
        summary.failed,
        summary.dry_run_would_create,
    )

    if not args.dry_run:
        log.info("Complaint and work-item create mode is active.")


if __name__ == "__main__":
    run()
