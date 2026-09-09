"""
create_compass_complaints.py

Sheet-driven Compass glass complaint and work-item batch creation.

Workflow (per complaint.md):
- Read spreadsheet rows for today (Inventory Date only)
- Validate MVA
- For each row occurrence:
  - Check existing OPEN Glass Repair/Replace complaint
    - Skip if the complaint and work item already exist
    - Create the missing complaint and/or work item
- Continue on errors
- Append run log and print summary
- Support --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
from config.config_loader import get_config


BASE_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_CONFIG_PATH = BASE_DIR / "orchestrator_config.json"
ORCHESTRATOR_PROJECT_CONFIG_PATH = BASE_DIR / "orchestrator_project.json"
ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_project.local.json"
ORCHESTRATOR_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_config.local.json"
SHARED_LOCAL_CONFIG_PATH = BASE_DIR / "config" / "config.local.json"

LOG_FILE = BASE_DIR / "EnsureGlassWorkItems.log"
COMPASS_HOME_URL = "https://avisbudget.palantirfoundry.com/workspace/module/view/latest/ri.workshop.main.module.d62ba12c-018c-41c1-8214-0749f6591b30"
COMPASS_GO_BASE_URL = "https://go.avisbudget.palantirfoundry.com"
COMPASS_VEHICLES_BUTTON_SELECTOR = "[data-test-id='workshop-inline-button']"
COMPASS_KEYWORD_SEARCH_INPUT_SELECTOR = "input[type='search'][placeholder='Keyword Search (other fields)']"
COMPASS_WORKSHOP_OBJECT_TABLE_SELECTOR = "[data-test-id='workshop-object-table']"
COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR = "[data-test-id='workshop-object-title']"
COMPASS_DETAILS_PANEL_TABLE_SELECTOR = "[data-test-id='ov-full-object-view-tabs-content'] [data-test-id='workshop-object-table']"
COMPASS_OVERVIEW_MVA_ROW_SELECTOR = (
    "xpath=//div[@role='listitem']"
    "[.//div[contains(@class,'property-display-name')]"
    "/div[normalize-space()='MVA']]"
)
COMPASS_OVERVIEW_MVA_VALUE_SELECTOR = (
    "div[class*='property-display-value'] span[class*='array-list-entry']"
)
BROWSER_PROFILE_DIR = BASE_DIR / "outlook" / "browser_profile"
SETTLE_WAIT_MS = 15_000
COMPASS_COMPLAINT_API_FLAG = "compass_complaint_api_precheck_enabled"
COMPASS_COMPLAINT_API_BASE_URL_KEY = "compass_complaint_api_base_url"
ACTIVE_GLASS_COMPLAINT_TYPES = {"glass damage"}
INACTIVE_COMPLAINT_STATUSES = {"ignored", "deleted", "resolved"}

log = logging.getLogger("EnsureGlassWorkItems")


@dataclass
class CandidateRow:
    row_index: int
    mva: str
    inventory_date_raw: str
    damage_expectation: str | None = None
    area: str | None = None


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
    source: str = "ui"
    complaint_ids: list[str] | None = None


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


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _normalize_compass_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _runtime_compass_api_base_url(runtime_config: dict[str, Any]) -> str:
    configured = str(runtime_config.get(COMPASS_COMPLAINT_API_BASE_URL_KEY, "")).strip()
    return (configured or COMPASS_GO_BASE_URL).rstrip("/")


def _prime_compass_go_scan_page(page, mva: str) -> None:
    try:
        if "Confirm User" in (page.locator("body").inner_text(timeout=2000) or ""):
            page.locator('input[autocomplete="one-time-code"]').first.fill("764567")
            page.wait_for_timeout(3500)

        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(5000)

        begin = page.get_by_role("button", name="Begin Scanning", exact=True)
        if begin.count() and begin.first.is_visible():
            begin.first.click()
            page.wait_for_timeout(1000)

        input_locator = page.get_by_label("Or enter MVA/VIN").first
        input_locator.wait_for(state="visible", timeout=30000)
        input_locator.fill(mva)
        page.get_by_role("button", name="Enter", exact=True).first.click()
        page.wait_for_timeout(8000)
    except Exception as exc:
        raise RuntimeError(f"go_scan_prime_failed: {exc}") from exc


def _post_compass_structure(page, url: str, payload: dict[str, Any]) -> Any:
    result = page.evaluate(
        """
        async ({ url, payload }) => {
            const form = new FormData();
            form.append('__structure__', JSON.stringify(payload));
            const response = await fetch(url, {
                method: 'POST',
                body: form,
                credentials: 'include',
            });
            const text = await response.text();
            return { ok: response.ok, status: response.status, text };
        }
        """,
        {"url": url, "payload": payload},
    )
    if not result.get("ok"):
        raise RuntimeError(f"request_failed status={result.get('status')} url={url}")
    return json.loads(result.get("text") or "null")


def _extract_vehicle_identifier(vehicle: dict[str, Any]) -> str | None:
    for key in ("$primaryKey", "primaryKey_", "primaryKey", "dwMvaNo"):
        value = str(vehicle.get(key, "")).strip()
        if value:
            return value
    return None


def _is_active_compass_complaint(complaint: dict[str, Any]) -> bool:
    status = _normalize_compass_text(complaint.get("status"))
    return status not in INACTIVE_COMPLAINT_STATUSES


def _is_glass_complaint_record(complaint: dict[str, Any]) -> bool:
    for key in ("damageCategory", "complaintDescription", "$title"):
        if _normalize_compass_text(complaint.get(key)) in ACTIVE_GLASS_COMPLAINT_TYPES:
            return True
    return False


def _inspect_glass_complaint_via_api(page, runtime_config: dict[str, Any], mva: str) -> LookupResult:
    base_url = _runtime_compass_api_base_url(runtime_config)
    vehicle_payload = {"where": {"mvaNo": mva}, "$pageSize": 1}
    vehicle_rows = _post_compass_structure(
        page,
        f"{base_url}/sw/get-vehicle-fresh",
        vehicle_payload,
    )
    if not isinstance(vehicle_rows, list) or not vehicle_rows:
        raise RuntimeError(f"vehicle_lookup_empty for {mva}")

    vehicle = vehicle_rows[0]
    if not isinstance(vehicle, dict):
        raise RuntimeError(f"vehicle_lookup_invalid for {mva}")

    vehicle_identifier = _extract_vehicle_identifier(vehicle)
    if not vehicle_identifier:
        raise RuntimeError(f"vehicle_identifier_missing for {mva}")

    complaint_payload = {
        "where": {
            "$and": [
                {"$or": [{"dwMvaNo": vehicle_identifier}, {"mvaNo": mva}]},
                {"$not": {"status": {"$in": ["Ignored", "Deleted", "Resolved"]}}},
            ]
        }
    }
    complaint_rows = _post_compass_structure(
        page,
        f"{base_url}/sw/get-complaint-fresh",
        complaint_payload,
    )
    if not isinstance(complaint_rows, list):
        raise RuntimeError(f"complaint_lookup_invalid for {mva}")

    active_rows = [row for row in complaint_rows if isinstance(row, dict) and _is_active_compass_complaint(row)]
    glass_rows = [row for row in active_rows if _is_glass_complaint_record(row)]
    matched_titles = [
        str(row.get("damageCategory") or row.get("complaintDescription") or row.get("$title") or "").strip()
        for row in glass_rows
        if str(row.get("damageCategory") or row.get("complaintDescription") or row.get("$title") or "").strip()
    ]
    complaint_ids = [
        str(row.get("complaintId") or row.get("$primaryKey") or "").strip()
        for row in glass_rows
        if str(row.get("complaintId") or row.get("$primaryKey") or "").strip()
    ]

    return LookupResult(
        exists=bool(glass_rows),
        reason="api_glass_complaint_found" if glass_rows else "api_glass_complaint_not_present",
        title_texts=matched_titles,
        source="api",
        complaint_ids=complaint_ids,
    )


def _resolve_glass_complaint_lookup(context, page, runtime_config: dict[str, Any], mva: str) -> LookupResult:
    use_api_precheck = _config_bool(runtime_config.get(COMPASS_COMPLAINT_API_FLAG), default=False)
    if not use_api_precheck:
        return _inspect_glass_complaint(page, mva)

    api_page = None
    try:
        api_page = context.new_page()
        api_page.goto(_runtime_compass_api_base_url(runtime_config), wait_until="domcontentloaded")
        api_page.wait_for_timeout(2_000)
        _handle_msft_auth_if_needed(api_page)
        _prime_compass_go_scan_page(api_page, mva)
        lookup = _inspect_glass_complaint_via_api(api_page, runtime_config, mva)
        log.info(
            "MVA %s -> complaint lookup via %s: exists=%s reason=%s complaint_ids=%s titles=%s",
            mva,
            lookup.source,
            lookup.exists,
            lookup.reason,
            lookup.complaint_ids or [],
            lookup.title_texts or [],
        )
        return lookup
    except Exception as exc:
        log.warning(
            "MVA %s -> API complaint lookup failed (%s); falling back to UI lookup",
            mva,
            exc,
        )
        fallback = _inspect_glass_complaint(page, mva)
        log.info(
            "MVA %s -> fallback UI lookup result: exists=%s reason=%s source=%s",
            mva,
            fallback.exists,
            fallback.reason,
            fallback.source,
        )
        return fallback
    finally:
        try:
            if api_page is not None:
                api_page.close()
        except Exception:
            pass


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


def _normalize_damage_expectation(raw: str) -> str | None:
    value = raw.strip().lower()
    if not value:
        return None

    is_repair = bool(re.search(r"\brepair\b|\bchip\b", value)) or value == "r"
    is_replace = bool(re.search(r"\breplace\b|\breplacement\b|\bcrack\b", value))

    if is_repair and not is_replace:
        return "repair"
    if is_replace and not is_repair:
        return "replace"
    return None


def _subcategory_for_damage_expectation(expectation: str | None) -> str | None:
    if expectation == "repair":
        return "Windshield Chip"
    if expectation == "replace":
        return "Windshield Crack"
    return None


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
    row_selector = f"div[class*='bp6-table-cell-row-{row_idx}'][class*='bp6-table-cell-col-0']"
    explicit = page.locator(
        f"{row_selector} input[aria-label='Select row'][type='checkbox']"
    ).first
    explicit.wait_for(state="visible", timeout=5000)
    explicit.check(force=True, timeout=5000)
    page.wait_for_timeout(150)
    return explicit.is_checked()


def _wait_for_work_item_dialog(page) -> tuple[bool, Any]:
    title = page.get_by_role(
        "heading",
        name=re.compile(r"^\s*Add Work Item to Complaint\(s\)\s*$", re.I),
    ).first
    title.wait_for(state="visible", timeout=10_000)
    scope = title.locator("xpath=ancestor::*[@role='region'][1]")
    return True, scope


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
        page.get_by_role("tab", name=re.compile(r"^Active Complaints", re.I)).first.click(timeout=4000)
        page.wait_for_timeout(300)

        row_idx = _find_glass_row_index_blueprint(page)
        if row_idx is None:
            return False, "glass_complaint_not_present"
        attached_count = _attached_work_items_count_blueprint(page, row_idx)
        if attached_count is None:
            raise RuntimeError("Glass complaint Attached Work Items count is unavailable")
        return attached_count > 0, f"glass_complaint_attached_work_items={attached_count}"
    except Exception as exc:
        return False, f"work_item_check_failed: {exc}"


def _open_home_page(page) -> None:
    page.goto(COMPASS_HOME_URL)
    page.wait_for_load_state("domcontentloaded")
    _handle_msft_auth_if_needed(page)
    log.info("Compass page after goto: url=%s title=%s", page.url, page.title())
    page.wait_for_timeout(SETTLE_WAIT_MS)


def _credentials() -> tuple[str, str, str]:
    """Resolve Microsoft SSO credentials from env first, then config."""
    username = str(os.getenv("GLASS_LOGIN_USERNAME") or get_config("username", "")).strip()
    password = str(os.getenv("GLASS_LOGIN_PASSWORD") or get_config("password", "")).strip()
    sso_email = str(get_config("credentials.sso_email", "")).strip()
    return username, password, sso_email


def _pick_sso_account_if_needed(page, sso_email: str) -> None:
    """Select account on Microsoft picker when it appears."""
    try:
        picker = page.locator(
            '[aria-label*="Pick an account"], [data-testid="sso-page-identifier"]'
        ).first
        if picker.count() == 0 or not picker.is_visible(timeout=3_000):
            return

        tile = None
        if sso_email:
            candidate = page.locator('[data-testid="account-tile"]').filter(has_text=sso_email).first
            if candidate.count() > 0:
                tile = candidate

        if tile is None:
            tile = page.locator('[data-testid="account-tile"]').first

        if tile.count() > 0:
            tile.click(timeout=8_000)
            page.wait_for_load_state("domcontentloaded")
            log.info("Selected SSO account tile")
    except Exception:
        # Optional step: continue and let downstream selectors reveal auth state.
        return


def _handle_msft_auth_if_needed(page) -> None:
    """Handle Microsoft auth prompts when present; no-op when already authenticated."""
    username, password, sso_email = _credentials()

    # Automatic-login relay page can take a moment before showing the next auth state.
    if "automatic-login" in (page.url or ""):
        page.wait_for_timeout(2_000)

    # If account picker appears first, resolve it.
    _pick_sso_account_if_needed(page, sso_email)

    # If login form is visible, attempt credentialed sign-in.
    try:
        login_input = page.locator('input[name="loginfmt"]').first
        if login_input.count() == 0 or not login_input.is_visible(timeout=3_000):
            return

        if not username or not password:
            log.info(
                "Microsoft login form detected but username/password are not configured; "
                "manual login is required for this run."
            )
            return

        log.info("Microsoft login form detected; attempting automated sign-in")
        login_input.fill(username, timeout=10_000)
        page.locator("#idSIButton9").first.click(timeout=10_000)

        password_input = page.locator('input[name="passwd"]').first
        password_input.wait_for(state="visible", timeout=10_000)
        password_input.fill(password, timeout=10_000)
        page.locator("#idSIButton9").first.click(timeout=10_000)

        # Optional "Stay signed in" prompt.
        try:
            page.locator("#idBtn_Back").first.click(timeout=3_000)
        except Exception:
            pass

        _pick_sso_account_if_needed(page, sso_email)
        page.wait_for_load_state("domcontentloaded")
        log.info("Microsoft sign-in flow completed")
    except Exception as exc:
        log.warning("Microsoft sign-in automation did not complete cleanly: %s", exc)


def _click_vehicles(page):
    vehicles_button = page.locator(COMPASS_VEHICLES_BUTTON_SELECTOR).filter(has_text="Vehicles").first
    vehicles_button.wait_for(state="visible", timeout=10_000)
    with page.expect_popup(timeout=10_000) as popup_info:
        vehicles_button.click()
    candidate_page = popup_info.value
    candidate_page.wait_for_load_state("domcontentloaded")
    log.info("Compass Vehicles click opened a popup/tab: %s", candidate_page.url)
    candidate_page.wait_for_timeout(700)
    candidate_page.wait_for_timeout(SETTLE_WAIT_MS)
    _wait_for_keyword_search_input(candidate_page, timeout_s=20)
    return candidate_page


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


def _normalize_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _vehicle_mva_matches(actual: str, expected: str) -> bool:
    return _normalize_digits(actual) == _normalize_digits(expected)


def _wait_for_vehicle_details_mva(page, mva: str, timeout_s: int = 20) -> None:
    mva_rows = page.locator(COMPASS_OVERVIEW_MVA_ROW_SELECTOR)
    deadline = time.monotonic() + timeout_s
    last_seen = ""

    while time.monotonic() < deadline:
        try:
            if mva_rows.count() == 1:
                values = mva_rows.first.locator(COMPASS_OVERVIEW_MVA_VALUE_SELECTOR)
                if values.count() == 1:
                    last_seen = (values.first.inner_text(timeout=1000) or "").strip()
                    if _vehicle_mva_matches(last_seen, mva):
                        log.info("MVA %s -> vehicle details confirmed", mva)
                        return
        except Exception:
            pass
        page.wait_for_timeout(400)

    raise RuntimeError(
        f"MVA {mva} vehicle details did not confirm requested MVA (last_seen={last_seen!r})"
    )


def _select_vehicle_search_result(page, mva: str) -> None:
    table = page.locator(COMPASS_WORKSHOP_OBJECT_TABLE_SELECTOR).first
    table.wait_for(state="visible", timeout=20_000)
    titles = table.locator(COMPASS_WORKSHOP_OBJECT_TITLE_SELECTOR).filter(
        has_text=re.compile(rf"^\s*{re.escape(mva)}\s*$", re.I)
    )
    deadline = time.monotonic() + 20
    result_count = 0

    while time.monotonic() < deadline:
        result_count = titles.count()
        if result_count == 1:
            title = titles.first
            if title.is_visible():
                title.click(timeout=8000)
                page.wait_for_timeout(1500)
                return
        page.wait_for_timeout(250)

    raise RuntimeError(f"Expected one exact search result for MVA {mva}, found {result_count}")


def _search_mva(page, mva: str) -> None:
    by_mva = page.get_by_role("button", name=re.compile(r"^Search\s+by\s+MVA$", re.I)).first
    by_mva.wait_for(state="visible", timeout=8000)
    by_mva.click(timeout=5000)

    keyword = _wait_for_keyword_search_input(page)
    keyword.click()
    keyword.fill(mva)
    keyword.press("Enter")

    _select_vehicle_search_result(page, mva)
    _wait_for_vehicle_details_mva(page, mva)


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


def _glass_damage_complaint_count(page) -> int:
    table = page.locator(COMPASS_DETAILS_PANEL_TABLE_SELECTOR).first
    try:
        title_texts = _read_title_texts(table)
    except Exception:
        return 0
    return sum(text.strip().lower() == "glass damage" for text in title_texts)


def _wait_for_new_glass_complaint(page, baseline_count: int, timeout_ms: int = SETTLE_WAIT_MS) -> bool:
    attempts = max(1, timeout_ms // 500)
    for _ in range(attempts):
        if _glass_damage_complaint_count(page) > baseline_count:
            return True
        page.wait_for_timeout(500)
    return _glass_damage_complaint_count(page) > baseline_count


def _click_button_by_name(page, pattern: str, timeout_ms: int = 10_000) -> None:
    page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=timeout_ms)


def _complaint_popup_scope(page):
    heading = page.get_by_role("heading", name=re.compile(r"create complaint for mva", re.I)).first
    heading.wait_for(state="visible", timeout=8_000)
    return heading.locator("xpath=ancestor::div[contains(@class,'workshop-section')][1]")


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


def _complaint_form_region(scope, heading_name: str):
    heading = scope.get_by_role(
        "heading",
        name=re.compile(rf"^\s*{re.escape(heading_name)}\s*$", re.I),
    ).first
    heading.wait_for(state="visible", timeout=10_000)
    return heading.locator("xpath=ancestor::*[@role='region'][1]")


def _set_glass_damage_category(scope) -> None:
    category_region = _complaint_form_region(scope, "Category")
    category_label = category_region.locator(
        "label", has_text=re.compile(r"^\s*Glass Damage\s*$", re.I)
    ).first
    category_label.wait_for(state="visible", timeout=10_000)
    category_label.click(timeout=10_000)

    category = category_region.locator("input[type='radio'][value='Glass Damage']").first
    category.wait_for(state="attached", timeout=10_000)
    if not category.is_checked():
        category.check(force=True)
    if not category.is_checked():
        raise RuntimeError("Could not set Category to Glass Damage")


def _fill_complaint_description(scope, text: str) -> None:
    description = scope.locator("textarea.bp6-text-area").first
    description.wait_for(state="visible", timeout=8_000)
    description.fill(text, timeout=8_000)
    if description.input_value().strip() != text:
        raise RuntimeError("Complaint Description did not retain the required value")


def _set_glass_damage_subcategory(scope, value: str = "Glass Damage") -> None:
    subcategory_region = _complaint_form_region(scope, "Sub-Category")
    subcategory_label = subcategory_region.locator(
        "label", has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
    ).first
    subcategory_label.wait_for(state="visible", timeout=10_000)
    subcategory_label.click(timeout=10_000)

    subcategory = subcategory_region.locator(
        f"input[type='radio'][value='{value}']"
    ).first
    subcategory.wait_for(state="attached", timeout=10_000)
    if not subcategory.is_checked():
        subcategory.check(force=True)
    if not subcategory.is_checked():
        raise RuntimeError(f"Could not set Sub-Category to {value}")


def _complaint_fields_for_area(area: str | None) -> tuple[str, str]:
    if (area or "").strip().casefold() in {"rvm", "rear view mirror"}:
        return "Mechanical Issue", "RVM"
    return "Glass Damage", "Glass Damage"


def _create_complaint_only(
    page,
    mva: str,
    damage_expectation: str | None = None,
    area: str | None = None,
) -> tuple[bool, str]:
    try:
        def _submit_state(submit_locator):
            return submit_locator.evaluate(
                """
                el => {
                    if (!el) {
                        return { enabled: false, aria: null, disabledAttr: false, className: "" };
                    }
                    const aria = el.getAttribute('aria-disabled');
                    const disabledAttr = el.hasAttribute('disabled');
                    const className = (el.getAttribute('class') || '').toLowerCase();
                    const classDisabled = className.includes('disabled');
                    const enabled = aria !== 'true' && !disabledAttr && !classDisabled;
                    return { enabled, aria, disabledAttr, className };
                }
                """
            )

        baseline_complaint_count = _glass_damage_complaint_count(page)
        _click_button_by_name(page, r"create\s*complaint")
        page.wait_for_timeout(1200)
        scope = _complaint_popup_scope(page)

        _set_yes_drivable(scope)
        page.wait_for_timeout(600)

        _set_glass_damage_category(scope)
        page.wait_for_timeout(600)

        subcategory, description = _complaint_fields_for_area(area)

        _set_glass_damage_subcategory(scope, subcategory)
        page.wait_for_timeout(600)

        _fill_complaint_description(scope, description)
        page.wait_for_timeout(500)

        # Confirmed markup is an anchor role=button that starts disabled.
        submit = scope.locator("a[role='button']", has_text="Submit Complaint").first
        submit.wait_for(state="visible", timeout=10_000)
        log.info(
            "Complaint form state before submit: drivable_yes=%s category_glass=%s subcategory_glass=%s",
            scope.locator("input[type='radio'][value='Yes']").first.is_checked(),
            _complaint_form_region(scope, "Category")
            .locator("input[type='radio'][value='Glass Damage']")
            .first.is_checked(),
            _complaint_form_region(scope, "Sub-Category")
            .locator("input[type='radio'][value='Glass Damage']")
            .first.is_checked(),
        )
        log.info("MVA %s -> chosen sub-category=%s description=%s", mva, subcategory, description)

        ready = False
        last_submit_state = {}
        for _ in range(30):
            submit = scope.locator("a[role='button']", has_text="Submit Complaint").first
            submit.wait_for(state="visible", timeout=10_000)
            last_submit_state = _submit_state(submit)
            if last_submit_state.get("enabled", False):
                ready = True
                break
            page.wait_for_timeout(500)

        if not ready:
            raise RuntimeError(
                "submit_not_enabled_after_form_fill: "
                f"aria={last_submit_state.get('aria')} disabled_attr={last_submit_state.get('disabledAttr')}"
            )

        try:
            submit.click(timeout=10_000)
        except Exception:
            submit.scroll_into_view_if_needed(timeout=5_000)
            submit.click(timeout=10_000, force=True)

        if _wait_for_new_glass_complaint(page, baseline_complaint_count):
            _wait_for_vehicle_details_mva(page, mva)
            new_count = _glass_damage_complaint_count(page)
            log.info(
                "MVA %s -> new Glass Damage complaint visible (count %d -> %d)",
                mva,
                baseline_complaint_count,
                new_count,
            )
            return True, "new_complaint_visible"

        current_count = _glass_damage_complaint_count(page)
        return False, (
            "submitted_but_no_new_complaint: "
            f"glass_damage_count={baseline_complaint_count}->{current_count}"
        )
    except Exception as exc:
        return False, f"complaint_create_failed: {exc}"


def _create_work_item_for_glass_complaint(page, mva: str) -> tuple[bool, str]:
    try:
        page.get_by_role("tab", name=re.compile(r"^Active Complaints", re.I)).first.click(timeout=5000)

        row_idx = _find_glass_row_index_blueprint(page)
        if row_idx is None:
            _capture_work_item_debug_artifacts(page, mva, "glass_row_missing")
            return False, "glass_damage_row_not_found_in_active_complaints"
        if not _select_glass_complaint_row_blueprint(page, row_idx):
            _capture_work_item_debug_artifacts(page, mva, "glass_checkbox_not_selected")
            return False, "failed_to_select_glass_complaint_checkbox"
        if not _is_add_work_item_selected_enabled(page):
            _capture_work_item_debug_artifacts(page, mva, "add_work_item_disabled")
            return False, "add_work_item_to_selected_complaints_disabled"

        add_button = page.get_by_role(
            "button",
            name=re.compile(r"add work item to selected complaints", re.I),
        ).first
        add_button.wait_for(state="visible", timeout=10_000)
        add_button.click(timeout=10_000)
        page.wait_for_timeout(1200)

        _, scope = _wait_for_work_item_dialog(page)
        combobox = scope.locator(
            "div[role='combobox'][aria-label='Select an option…']"
        ).first
        combobox.wait_for(state="visible", timeout=5000)
        combobox.click(timeout=5000)

        exact_opcode_pattern = re.compile(r"^glass\s*repair\s*/\s*replace$", re.I)
        popup_search = page.locator(
            "div.bp6-popover-content input.bp6-input[placeholder='Search…']"
        ).last
        popup_search.wait_for(state="visible", timeout=4000)
        popup_search.fill("glass", timeout=4000)
        if (popup_search.input_value(timeout=2000) or "").strip().lower() != "glass":
            return False, "opcode_search_input_not_set"
        page.wait_for_timeout(900)

        listbox = page.locator(
            "ul[role='listbox'][aria-label='Select an option…']"
        ).first
        option = listbox.locator("li[role='option']").filter(has_text=exact_opcode_pattern)
        if option.count() != 1:
            return False, f"expected_one_glass_opcode_found_{option.count()}"
        option.first.wait_for(state="visible", timeout=4000)
        option.first.click(timeout=6000)

        page.wait_for_timeout(700)
        if not exact_opcode_pattern.fullmatch((combobox.inner_text(timeout=1000) or "").strip()):
            _capture_work_item_debug_artifacts(page, mva, "opcode_value_not_confirmed")
            return False, "glass_opcode_not_confirmed"

        create_button = scope.get_by_role("button", name="Create Work Item", exact=True).first
        create_button.wait_for(state="visible", timeout=15_000)
        if not _is_button_enabled(create_button):
            page.wait_for_timeout(1200)
        if not _is_button_enabled(create_button):
            _capture_work_item_debug_artifacts(page, mva, "create_work_item_disabled")
            return False, "create_work_item_disabled"

        create_button.click(timeout=10_000)
        toast = page.get_by_text("Successfully created new work item", exact=True).first
        toast.wait_for(state="visible", timeout=12_000)
        _wait_for_vehicle_details_mva(page, mva)
        return True, "work_item_created_toast_confirmed"
    except Exception as exc:
        return False, f"work_item_create_failed: {exc}"


def _collect_candidates(values: list[list[str]], run_day: date) -> tuple[list[CandidateRow], RunSummary]:
    if not values:
        raise RuntimeError("Sheet is empty.")

    headers = values[0]
    inv_col = _find_col(headers, "Inventory Date")
    mva_col = _find_col(headers, "MVA")
    damage_col = _find_col(
        headers,
        "DamageType",
        "Damage Type",
        "Repair/Replace",
        "Repair Replace",
        "Repair Or Replace",
        "Repair / Replace",
    )
    area_col = _find_col(headers, "Area", "Glass Area", "Damage Area")

    missing = []
    if inv_col is None:
        missing.append("Inventory Date")
    if mva_col is None:
        missing.append("MVA")
    if missing:
        raise RuntimeError(f"Required column(s) missing: {', '.join(missing)}")

    summary = RunSummary(total_rows_read=max(0, len(values) - 1))
    candidates: list[CandidateRow] = []
    seen_mvas: set[str] = set()

    for row_idx, row in enumerate(values[1:], start=2):
        inv_raw = row[inv_col].strip() if len(row) > inv_col else ""
        inv_date = _parse_sheet_date(inv_raw)
        if inv_date != run_day:
            continue

        summary.rows_for_day += 1

        mva_raw = row[mva_col].strip() if len(row) > mva_col else ""
        damage_raw = row[damage_col].strip() if (damage_col is not None and len(row) > damage_col) else ""
        area_raw = row[area_col].strip() if (area_col is not None and len(row) > area_col) else ""
        damage_expectation = _normalize_damage_expectation(damage_raw)
        if not inv_raw or inv_date is None or not _is_valid_mva(mva_raw):
            summary.skipped_invalid += 1
            log.info(
                "Row %d skipped invalid data: Inventory Date='%s', MVA='%s'",
                row_idx,
                inv_raw,
                mva_raw,
            )
            continue

        mva = _normalize_mva(mva_raw)
        if mva in seen_mvas:
            log.warning("Row %d skipped duplicate MVA %s; first occurrence will be processed", row_idx, mva)
            continue
        seen_mvas.add(mva)

        candidates.append(
            CandidateRow(
                row_index=row_idx,
                mva=mva,
                inventory_date_raw=inv_raw,
                damage_expectation=damage_expectation,
                area=area_raw or None,
            )
        )

    return candidates, summary


def _build_single_candidate(raw_mva: str) -> tuple[list[CandidateRow], RunSummary]:
    mva = _normalize_mva(raw_mva)
    if not _is_valid_mva(mva):
        raise RuntimeError(f"Invalid MVA override: {raw_mva!r}")
    summary = RunSummary(total_rows_read=1, rows_for_day=1)
    return [CandidateRow(row_index=1, mva=mva, inventory_date_raw=str(date.today()), damage_expectation=None)], summary


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
    4) Select the area-specific Sub-Category
    5) Enter the area-specific Complaint Description
    6) Click Submit Complaint
    7) Confirm a new Glass Damage complaint row is visible

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
            return _create_complaint_only(page, mva, damage_expectation=None)
        except Exception as exc:
            return False, f"create_flow_failed: {exc}"
        finally:
            context.close()


def _process_candidates(
    candidates: list[CandidateRow],
    dry_run: bool,
    summary: RunSummary,
    runtime_config: dict[str, Any] | None = None,
) -> RunSummary:
    if not candidates:
        log.info("No eligible MVAs to process")
        return summary

    runtime_config = runtime_config or {}

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
                    lookup = _resolve_glass_complaint_lookup(context, page, runtime_config, item.mva)
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
                        complaint_created, complaint_reason = _create_complaint_only(
                            page,
                            item.mva,
                            damage_expectation=item.damage_expectation,
                            area=item.area,
                        )
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


def run() -> int:
    _setup_logging()
    args = _parse_args()
    runtime_config = _load_runtime_config()

    run_mode = "DRY-RUN" if args.dry_run else "LIVE"
    log.info("Starting EnsureGlassWorkItems (%s)", run_mode)
    log.info("Initial Compass page: %s", COMPASS_HOME_URL)

    if args.mva:
        candidates, summary = _build_single_candidate(args.mva)
        log.info("Prototype MVA override enabled: %s", candidates[0].mva)
    else:
        spreadsheet_id = str(runtime_config.get("spreadsheet_id", "")).strip()
        sheet_name = str(runtime_config.get("sheet_name", "GlassClaims")).strip() or "GlassClaims"
        service_account_path = _resolve_path(str(runtime_config.get("service_account_json", "Service_account.json")))

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

    summary = _process_candidates(candidates, args.dry_run, summary, runtime_config=runtime_config)

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

    return 1 if summary.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(run())
