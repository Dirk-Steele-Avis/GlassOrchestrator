"""Populate GlassClaims NextAction values from FieldPO by MVA.

Scope rules (per operator workflow):
1. Process only rows where Action is replacement.
2. Look up rows where NextAction is blank, Error, or missing.
3. Verify rows where NextAction is non-blank and not a retry value.
3. Process only rows with Inventory Date equal to today.

Write behavior:
- Exactly one FPO found: write the raw FPO value.
- Multiple FPOs found: write "multi" for manual review.
- No FPO found: write "missing".
- FieldPO lookup error: write "Error".

Run with:
    .venv\\Scripts\\python.exe FieldPOFillNextAction.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import gspread  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise ModuleNotFoundError(
        "Missing dependency 'gspread'. Run with the project venv and install requirements."
    ) from exc

from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_CONFIG_PATH = BASE_DIR / "orchestrator_config.json"
ORCHESTRATOR_PROJECT_CONFIG_PATH = BASE_DIR / "orchestrator_project.json"
ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_project.local.json"
ORCHESTRATOR_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_config.local.json"
SHARED_LOCAL_CONFIG_PATH = BASE_DIR / "config" / "config.local.json"

FIELDPO_URL = "https://supply-chain.east.prod.sdp.abg.cloud/fieldpo/dashboard"
FIELDPO_PROFILE_DIR = BASE_DIR / "outlook" / "browser_profile"
BACKUP_DIR = BASE_DIR / "log" / "fieldpo_nextaction_backups"

log = logging.getLogger("FieldPOFillNextAction")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(BASE_DIR / "FieldPOFillNextAction.log"),
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


def _is_replacement(action_value: str) -> bool:
    normalized = action_value.strip().lower()
    return normalized in {
        "replacement",
        "replace",
        "replace(agn)",
        "replace (agn)",
    }


def _dismiss_attention_popup(page) -> None:
    page.wait_for_timeout(1500)
    resume_button = page.locator("button:has-text('Resume Work')")
    if resume_button.count() > 0 and resume_button.first.is_visible():
        log.info("FieldPO popup detected. Clicking Resume Work.")
        resume_button.first.click()
        page.wait_for_timeout(300)


def _ensure_fieldpo_login(page) -> None:
    page.goto(FIELDPO_URL)
    try:
        page.wait_for_url("**/fieldpo/dashboard**", timeout=30000)
    except Exception:
        log.warning("Dashboard did not load automatically. Please login in the opened browser.")
        input("After login reaches dashboard, press Enter to continue...")
        page.goto(FIELDPO_URL)
        page.wait_for_url("**/fieldpo/dashboard**", timeout=120000)
    _dismiss_attention_popup(page)


def _go_to_active_work_order_tab(page, mva: str) -> bool:
    page.goto(FIELDPO_URL)
    _dismiss_attention_popup(page)

    page.get_by_text("Search", exact=True).click()
    _dismiss_attention_popup(page)

    page.get_by_placeholder("WO#, PO#, MVA, VIN").fill(mva)
    page.get_by_role("button", name="Search").click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(5000)

    if page.locator("text=MVA#").count() == 0:
        log.info("%s - FieldPO returned no workorder result. Writing missing.", mva)
        return False

    page.locator("text=MVA#").first.click()

    _dismiss_attention_popup(page)
    page.wait_for_timeout(800)

    page.get_by_text("Active Work Order", exact=False).click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(1000)
    return True


def _extract_fpo_values(page) -> list[str]:
    # Scope extraction to the Purchase Orders section to avoid counting values
    # from unrelated page regions (history, hidden DOM, etc.).
    po_section_text = page.locator("text=Purchase Orders").first.locator("..").inner_text()

    count_match = re.search(r"Purchase\s+Orders\s*\[(\d+)\]", po_section_text, re.IGNORECASE)
    declared_count = int(count_match.group(1)) if count_match else None

    # In this UI the PO card line is rendered as "PO# FPO1079594".
    po_pattern = re.compile(r"\bPO\s*#\s*([A-Z0-9\-]+)\b", re.IGNORECASE)
    found: list[str] = [str(match).strip() for match in po_pattern.findall(po_section_text)]
    found = [value for value in found if value and value.upper() not in {"PO", "FPO"}]

    # Preserve discovery order while de-duplicating.
    deduped: list[str] = []
    seen: set[str] = set()
    for value in found:
        key = value.upper()
        if key not in seen:
            seen.add(key)
            deduped.append(value)

    # Business rule update: an MVA can have multiple workorders, and the
    # topmost workorder should be treated as the source of truth.
    # We therefore take the first PO value shown in the section.
    if deduped:
        if declared_count is not None and declared_count > 1:
            log.info(
                "Purchase Orders count is %d; selecting topmost PO value %s",
                declared_count,
                deduped[0],
            )
        return [deduped[0]]

    return []


def _page_looks_like_no_fpo(page) -> bool:
    """Best-effort check for a workorder with no purchase order / no FPO."""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False

    normalized = body_text.lower()
    if "purchase orders [0]" in normalized:
        return True
    if "no purchase orders" in normalized:
        return True
    if "po#" not in normalized and "purchase orders" in normalized:
        return True
    return False


def _resolve_next_action(page, mva: str) -> str:
    try:
        if not _go_to_active_work_order_tab(page, mva):
            return "missing"
        fpos = _extract_fpo_values(page)
    except Exception as exc:
        if _page_looks_like_no_fpo(page):
            log.info("%s - No FPO found in FieldPO. Writing missing.", mva)
            return "missing"

        log.warning("%s - FieldPO lookup failed (%s). Writing Error.", mva, exc)
        return "Error"

    if not fpos:
        return "missing"
    if len(fpos) > 1:
        return "multi"
    resolved = fpos[0].strip()
    return resolved or "missing"


def _next_action_cell(row: list[str], next_action_col_1based: int) -> str:
    idx = next_action_col_1based - 1
    if len(row) <= idx:
        return ""
    return row[idx].strip()


def _is_retry_value(value: str) -> bool:
    """Return True when an existing NextAction should be retried."""
    lowered = value.strip().lower()
    return lowered in {"error", "missing"}


def _collect_candidates(values: list[list[str]]) -> tuple[list[tuple[int, str]], int]:
    if not values:
        raise RuntimeError("Sheet is empty.")

    headers = values[0]
    mva_col = _find_col(headers, "MVA")
    action_col = _find_col(headers, "Action", "Damage Type", "damage_type")
    inventory_col = _find_col(headers, "Inventory Date", "Arrival Date")
    next_action_col = _find_col(headers, "NextAction", "Next Action")

    missing = []
    if mva_col is None:
        missing.append("MVA")
    if action_col is None:
        missing.append("Action")
    if inventory_col is None:
        missing.append("Inventory Date/Arrival Date")
    if next_action_col is None:
        missing.append("NextAction")
    if missing:
        raise RuntimeError(f"Required column(s) missing: {', '.join(missing)}")

    today = date.today()
    candidates: list[tuple[int, str]] = []

    for row_idx, row in enumerate(values[1:], start=2):
        mva = row[mva_col].strip() if len(row) > mva_col else ""
        if not mva:
            continue

        action = row[action_col].strip() if len(row) > action_col else ""
        if not _is_replacement(action):
            continue

        next_action = row[next_action_col].strip() if len(row) > next_action_col else ""
        if next_action and not _is_retry_value(next_action):
            continue

        inv_raw = row[inventory_col].strip() if len(row) > inventory_col else ""
        inv_date = _parse_sheet_date(inv_raw)
        if inv_date != today:
            continue

        candidates.append((row_idx, mva))

    return candidates, next_action_col + 1  # convert to 1-based for update_cell


def _collect_non_blank_verification_rows(
    values: list[list[str]],
    next_action_col_1based: int,
) -> list[tuple[int, str, str]]:
    """Collect today's replacement rows where NextAction is already non-blank.

    These rows are used for verification only (no overwrite).
    """
    if not values:
        return []

    headers = values[0]
    mva_col = _find_col(headers, "MVA")
    action_col = _find_col(headers, "Action", "Damage Type", "damage_type")
    inventory_col = _find_col(headers, "Inventory Date", "Arrival Date")
    if mva_col is None or action_col is None or inventory_col is None:
        return []

    today = date.today()
    verify_rows: list[tuple[int, str, str]] = []
    next_action_col = next_action_col_1based - 1

    for row_idx, row in enumerate(values[1:], start=2):
        mva = row[mva_col].strip() if len(row) > mva_col else ""
        if not mva:
            continue

        action = row[action_col].strip() if len(row) > action_col else ""
        if not _is_replacement(action):
            continue

        inv_raw = row[inventory_col].strip() if len(row) > inventory_col else ""
        inv_date = _parse_sheet_date(inv_raw)
        if inv_date != today:
            continue

        existing = row[next_action_col].strip() if len(row) > next_action_col else ""
        if not existing:
            continue
        if _is_retry_value(existing):
            continue

        verify_rows.append((row_idx, mva, existing))

    return verify_rows


def _collect_in_scope_blank_rows(
    values: list[list[str]],
    next_action_col_1based: int,
) -> list[tuple[int, str]]:
    """Collect today's replacement rows where NextAction is still blank."""
    if not values:
        return []

    headers = values[0]
    mva_col = _find_col(headers, "MVA")
    action_col = _find_col(headers, "Action", "Damage Type", "damage_type")
    inventory_col = _find_col(headers, "Inventory Date", "Arrival Date")
    if mva_col is None or action_col is None or inventory_col is None:
        return []

    today = date.today()
    out: list[tuple[int, str]] = []
    next_action_col = next_action_col_1based - 1

    for row_idx, row in enumerate(values[1:], start=2):
        mva = row[mva_col].strip() if len(row) > mva_col else ""
        if not mva:
            continue

        action = row[action_col].strip() if len(row) > action_col else ""
        if not _is_replacement(action):
            continue

        inv_raw = row[inventory_col].strip() if len(row) > inventory_col else ""
        inv_date = _parse_sheet_date(inv_raw)
        if inv_date != today:
            continue

        next_action = row[next_action_col].strip() if len(row) > next_action_col else ""
        if next_action:
            continue

        out.append((row_idx, mva))

    return out


def _normalize_next_action_for_compare(value: str) -> str:
    """Normalize NextAction values for stable comparisons."""
    raw = str(value).strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered == "error":
        return "Error"
    if lowered == "missing":
        return "missing"
    if lowered == "multi":
        return "multi"

    return raw.upper()


def _write_backup(
    values: list[list[str]],
    candidates: list[tuple[int, str]],
    next_action_col_1based: int,
    spreadsheet_id: str,
    sheet_name: str,
) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"nextaction_backup_{timestamp}.json"

    rows_payload: list[dict[str, Any]] = []
    for row_idx, mva in candidates:
        sheet_row = values[row_idx - 1] if row_idx - 1 < len(values) else []
        rows_payload.append(
            {
                "row_index": row_idx,
                "mva": mva,
                "previous_next_action": _next_action_cell(sheet_row, next_action_col_1based),
            }
        )

    payload = {
        "created_at": datetime.now().isoformat(),
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "next_action_column_1based": next_action_col_1based,
        "rows": rows_payload,
    }
    backup_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return backup_path


def _apply_rollback(ws, backup_path: Path) -> None:
    if not backup_path.exists():
        raise RuntimeError(f"Rollback file not found: {backup_path}")

    try:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid rollback JSON: {backup_path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid rollback payload: {backup_path}")

    rows = payload.get("rows", [])
    next_action_col_1based = payload.get("next_action_column_1based")
    if not isinstance(rows, list) or not isinstance(next_action_col_1based, int):
        raise RuntimeError(f"Rollback file missing required keys: {backup_path}")

    headers = ws.row_values(1)
    mva_col_idx = _find_col(headers, "MVA")

    updated = 0
    skipped = 0
    for row_info in rows:
        if not isinstance(row_info, dict):
            skipped += 1
            continue

        row_idx = row_info.get("row_index")
        expected_mva = str(row_info.get("mva", "")).strip()
        previous_value = str(row_info.get("previous_next_action", ""))
        if not isinstance(row_idx, int):
            skipped += 1
            continue

        row_values = ws.row_values(row_idx)
        current_mva = ""
        if mva_col_idx is not None and len(row_values) > mva_col_idx:
            current_mva = row_values[mva_col_idx].strip()

        if expected_mva and current_mva and expected_mva != current_mva:
            log.warning(
                "Rollback skip row %d: MVA mismatch backup=%s current=%s",
                row_idx,
                expected_mva,
                current_mva,
            )
            skipped += 1
            continue

        ws.update_cell(row_idx, next_action_col_1based, previous_value)
        updated += 1

    log.info("Rollback complete. Restored=%d, skipped=%d", updated, skipped)


def _latest_backup_file() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    candidates = sorted(BACKUP_DIR.glob("nextaction_backup_*.json"))
    if not candidates:
        return None
    return candidates[-1]


def _safety_fill_missing_for_blanks(ws, row_indexes: list[int], next_action_col_1based: int) -> int:
    """Ensure processed rows never remain blank in NextAction.

    Returns number of cells patched to "missing".
    """
    patched = 0
    for row_idx in row_indexes:
        row_values = ws.row_values(row_idx)
        current = ""
        if len(row_values) >= next_action_col_1based:
            current = row_values[next_action_col_1based - 1].strip()
        if current:
            continue
        ws.update_cell(row_idx, next_action_col_1based, "missing")
        patched += 1
        log.warning("Row %d had blank NextAction after processing. Patched to missing.", row_idx)
    return patched


def _read_next_action_value(ws, row_idx: int, next_action_col_1based: int) -> str:
    """Read one sheet cell value from the NextAction column."""
    row_values = ws.row_values(row_idx)
    if len(row_values) < next_action_col_1based:
        return ""
    return row_values[next_action_col_1based - 1].strip()


def _write_next_action_if_blank(ws, row_idx: int, next_action_col_1based: int, value: str) -> tuple[bool, str]:
    """Write NextAction only when blank or retry value; preserve other existing values.

    Returns (written, final_value).
    """
    current = _read_next_action_value(ws, row_idx, next_action_col_1based)
    if current and not _is_retry_value(current):
        return False, current

    final_value = str(value).strip() or "missing"
    ws.update_cell(row_idx, next_action_col_1based, final_value)
    return True, final_value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill NextAction from FieldPO or rollback prior updates")
    parser.add_argument(
        "--rollback-file",
        help="Path to a backup JSON file created by this script; restores previous NextAction values.",
    )
    parser.add_argument(
        "--rollback-latest",
        action="store_true",
        help="Restore from the latest backup file in log/fieldpo_nextaction_backups.",
    )
    return parser.parse_args()


def run() -> None:
    _setup_logging()
    log.info("Starting FieldPO NextAction fill script")
    args = _parse_args()

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

    if args.rollback_file and args.rollback_latest:
        raise RuntimeError("Use either --rollback-file or --rollback-latest, not both.")

    if args.rollback_file:
        _apply_rollback(ws, Path(args.rollback_file))
        return
    if args.rollback_latest:
        latest = _latest_backup_file()
        if latest is None:
            raise RuntimeError("No backup files found for --rollback-latest.")
        log.info("Using latest backup: %s", latest)
        _apply_rollback(ws, latest)
        return

    values = ws.get_all_values()
    candidates, next_action_col = _collect_candidates(values)
    verify_rows = _collect_non_blank_verification_rows(values, next_action_col)
    log.info(
        "Found %d lookup candidate row(s) and %d non-blank verification row(s) for today.",
        len(candidates),
        len(verify_rows),
    )
    if not candidates and not verify_rows:
        log.info("No eligible rows to update or verify. Exiting.")
        return

    if candidates:
        backup_path = _write_backup(values, candidates, next_action_col, spreadsheet_id, sheet_name)
        log.info("Backup written: %s", backup_path)

    FIELDPO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    cache: dict[str, str] = {}
    updated = 0
    multi = 0
    missing = 0
    processed_rows: list[int] = [row_idx for row_idx, _ in candidates]
    verify_match = 0
    verify_mismatch = 0

    def _record_value(value: str) -> None:
        nonlocal updated, multi, missing
        updated += 1
        if value == "multi":
            multi += 1
        elif value == "missing":
            missing += 1

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(FIELDPO_PROFILE_DIR), headless=False)
        try:
            page = context.new_page()
            _ensure_fieldpo_login(page)

            # Single pass: process all candidate rows from the initial snapshot.
            for row_idx, mva in candidates:
                if mva in cache:
                    result = cache[mva]
                else:
                    log.info("Lookup MVA %s", mva)
                    result = _resolve_next_action(page, mva)
                    cache[mva] = result

                result = str(result).strip() or "missing"

                written, final_value = _write_next_action_if_blank(ws, row_idx, next_action_col, result)
                if written:
                    _record_value(final_value)
                    log.info("Row %d MVA %s -> NextAction=%s", row_idx, mva, final_value)
                else:
                    log.info(
                        "Row %d MVA %s already had NextAction=%s. Preserved existing value.",
                        row_idx,
                        mva,
                        final_value,
                    )

            for row_idx, mva, existing_value in verify_rows:
                if mva in cache:
                    resolved = cache[mva]
                else:
                    log.info("Verify lookup MVA %s", mva)
                    resolved = _resolve_next_action(page, mva)
                    cache[mva] = resolved

                expected = _normalize_next_action_for_compare(existing_value)
                actual = _normalize_next_action_for_compare(resolved)

                if expected == actual:
                    verify_match += 1
                    log.info(
                        "Verify row %d MVA %s matched existing NextAction=%s",
                        row_idx,
                        mva,
                        existing_value,
                    )
                else:
                    verify_mismatch += 1
                    log.warning(
                        "Verify row %d MVA %s mismatch: sheet=%s fieldpo=%s",
                        row_idx,
                        mva,
                        existing_value,
                        resolved,
                    )
        finally:
            context.close()

    patched_blanks = _safety_fill_missing_for_blanks(ws, processed_rows, next_action_col)

    # Final hard guard: no in-scope rows should remain blank.
    # This catches rows missed by transient sheet/write edge cases.
    remaining_values = ws.get_all_values()
    remaining_blanks = _collect_in_scope_blank_rows(remaining_values, next_action_col)
    forced_scope_fills = 0
    for row_idx, mva in remaining_blanks:
        ws.update_cell(row_idx, next_action_col, "missing")
        forced_scope_fills += 1
        log.warning("Final guard patched row %d MVA %s to missing.", row_idx, mva)

    log.info(
        "Complete. Updated=%d, multi=%d, missing=%d, single=%d, patched_blanks=%d, forced_scope_fills=%d, verify_match=%d, verify_mismatch=%d",
        updated,
        multi,
        missing,
        updated - multi - missing,
        patched_blanks,
        forced_scope_fills,
        verify_match,
        verify_mismatch,
    )


if __name__ == "__main__":
    run()
