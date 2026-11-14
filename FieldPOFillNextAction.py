r"""Populate GlassClaims NextAction values from FieldPO by MVA.

Scope rules (per operator workflow):
1. Process only rows where Action is replacement.
2. Look up rows where NextAction is blank, Error, or missing.
3. Verify rows where NextAction is non-blank and not a retry value.
4. Process only rows with the explicitly selected Inventory Date.

Write behavior (never guess):
- Only a FieldPO search result whose MVA exactly equals the row MVA is opened.
- The vehicle's FieldPO VIN must equal the row VIN, otherwise write "review".
- A PO counts only when its card shows "Created by Steele, Dirk" and its
  created date is on or after the row's Inventory Date.
- One or more eligible POs: write them, comma-separated (one repair can have
  e.g. a parts/labor PO and a calibration PO; closeout matches each invoice to
  its PO by price).
- No eligible PO: write "missing".
- A verified sheet PO that no longer matches FieldPO: write "review".
- FieldPO lookup error: stop the workflow.

Run with:
    .venv\Scripts\python.exe FieldPOFillNextAction.py --inventory-date YYYY-MM-DD
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
REQUIRED_PO_CREATOR = "Steele, Dirk"
REVIEW_VALUE = "review"
_VIN_PATTERN = re.compile(r"\bVIN\s*#?\s*:?\s*([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
_MVA_RESULT_PATTERN = re.compile(r"\bMVA\s*#?\s*:?\s*(\d{8,9})\b", re.IGNORECASE)
_PO_LIST_PATTERN = re.compile(r"^\s*FPO\d+(\s*,\s*FPO\d+)*\s*$", re.IGNORECASE)
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
    page.wait_for_url("**/fieldpo/dashboard**", timeout=30000)
    _dismiss_attention_popup(page)


def _normalize_person(value: str) -> str:
    """Compare names ignoring case and spacing ("Steele,Dirk" == "Steele, Dirk")."""
    return re.sub(r"\s+", "", value).casefold()


def _normalize_mva(value: str) -> str:
    raw = str(value or "").strip()
    return raw.zfill(9) if re.fullmatch(r"\d{8,9}", raw) else raw


def _normalize_vin(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return normalized if len(normalized) == 17 else ""


def _select_exact_mva_result(result_texts: list[str], mva: str) -> int | None:
    """Return the index of the first search result whose MVA equals ``mva``.

    FieldPO search can return results for other vehicles (partial matches), so
    a result is only usable when its displayed MVA is an exact match.
    """
    target = _normalize_mva(mva)
    for idx, text in enumerate(result_texts):
        found = {_normalize_mva(value) for value in _MVA_RESULT_PATTERN.findall(text)}
        if found == {target}:
            return idx
    return None


def _extract_vin_from_text(text: str) -> str:
    """Return the single VIN shown in ``text``, or "" when absent or ambiguous."""
    vins = {match.upper() for match in _VIN_PATTERN.findall(text)}
    return next(iter(vins)) if len(vins) == 1 else ""


def _open_vehicle(page, mva: str) -> str:
    """Open the exact-MVA FieldPO vehicle and return its VIN.

    Returns "" when FieldPO has no result for exactly this MVA.
    Leaves the page on the Vehicle Details tab.
    """
    page.goto(FIELDPO_URL)
    _dismiss_attention_popup(page)

    page.get_by_text("Search", exact=True).click()
    _dismiss_attention_popup(page)

    page.get_by_placeholder("WO#, PO#, MVA, VIN").fill(mva)
    page.get_by_role("button", name="Search").click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(5000)

    results = page.locator("text=MVA#")
    result_texts = [
        results.nth(idx).locator("xpath=..").inner_text() for idx in range(results.count())
    ]
    selected = _select_exact_mva_result(result_texts, mva)
    if selected is None:
        log.info(
            "%s - FieldPO returned no result with this exact MVA (%d result(s) total).",
            mva,
            len(result_texts),
        )
        return ""

    results.nth(selected).click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(800)

    page.get_by_text("Vehicle Details", exact=True).click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(800)
    vin = _extract_vin_from_text(page.locator("body").inner_text())
    if not vin:
        raise RuntimeError(f"FieldPO vehicle page for MVA {mva} does not show exactly one VIN")
    return vin


def _go_to_active_work_order_tab(page) -> None:
    page.get_by_text("Active Work Order", exact=False).click()
    _dismiss_attention_popup(page)
    page.wait_for_timeout(1000)


def _extract_fpo_values_from_text(
    po_section_text: str,
    min_created_on: date | None = None,
) -> list[str]:
    """Extract every eligible FPO value from the Purchase Orders section text.

    A PO only counts when its own card shows it was created by
    REQUIRED_PO_CREATOR and, when ``min_created_on`` is given, was created on
    or after that date (older POs belong to an earlier repair). One repair can
    legitimately have several POs (e.g. parts/labor and calibration), so all
    eligible values are returned in page order.
    """
    # In this UI the PO card line is rendered as "PO# FPO1079594", and each
    # card carries its own "Created by <name> on YYYY-MM-DD" line.
    po_pattern = re.compile(r"\bPO\s*#\s*([A-Z0-9\-]+)\b", re.IGNORECASE)
    creator_pattern = re.compile(
        r"Created\s+by\s+(.+?)\s+on\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE
    )

    po_matches = [
        match
        for match in po_pattern.finditer(po_section_text)
        if match.group(1).strip() and match.group(1).strip().upper() not in {"PO", "FPO"}
    ]

    eligible: list[str] = []
    for idx, match in enumerate(po_matches):
        po_value = match.group(1).strip()
        segment_end = (
            po_matches[idx + 1].start() if idx + 1 < len(po_matches) else len(po_section_text)
        )
        segment = po_section_text[match.end():segment_end]
        creator_match = creator_pattern.search(segment)
        creator = creator_match.group(1).strip() if creator_match else ""

        if _normalize_person(creator) != _normalize_person(REQUIRED_PO_CREATOR):
            log.info(
                "PO %s skipped: created by %s, not %s.",
                po_value,
                creator or "<missing>",
                REQUIRED_PO_CREATOR,
            )
            continue

        if min_created_on is not None:
            created_on = datetime.strptime(creator_match.group(2), "%Y-%m-%d").date()
            if created_on < min_created_on:
                log.info(
                    "PO %s skipped: created %s, before Inventory Date %s.",
                    po_value,
                    created_on.isoformat(),
                    min_created_on.isoformat(),
                )
                continue

        eligible.append(po_value)

    # Preserve discovery order while de-duplicating.
    deduped: list[str] = []
    seen: set[str] = set()
    for value in eligible:
        key = value.upper()
        if key not in seen:
            seen.add(key)
            deduped.append(value)

    return deduped


def _extract_fpo_values(page, min_created_on: date | None = None) -> list[str]:
    # Scope extraction to the Purchase Orders section to avoid counting values
    # from unrelated page regions (history, hidden DOM, etc.).
    po_section_text = page.locator("text=Purchase Orders").first.locator("..").inner_text()
    return _extract_fpo_values_from_text(po_section_text, min_created_on)


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


def _resolve_next_action(page, mva: str, expected_vin: str, inventory_date: date) -> str:
    """Return the NextAction value for one sheet row; never guess.

    "missing" = FieldPO has no eligible PO for exactly this vehicle.
    "review"  = FieldPO shows a different vehicle than the sheet row.
    Otherwise the eligible PO number(s), comma-separated.
    """
    sheet_vin = _normalize_vin(expected_vin)
    if not sheet_vin:
        log.warning("%s - Sheet VIN %r is not a valid VIN. Writing review.", mva, expected_vin)
        return REVIEW_VALUE

    try:
        fieldpo_vin = _open_vehicle(page, mva)
        if not fieldpo_vin:
            return "missing"
        if fieldpo_vin != sheet_vin:
            log.warning(
                "%s - FieldPO VIN %s does not match sheet VIN %s. Writing review.",
                mva,
                fieldpo_vin,
                sheet_vin,
            )
            return REVIEW_VALUE
        _go_to_active_work_order_tab(page)
        fpos = _extract_fpo_values(page, inventory_date)
    except Exception as exc:
        if _page_looks_like_no_fpo(page):
            log.info("%s - No FPO found in FieldPO. Writing missing.", mva)
            return "missing"

        raise RuntimeError(f"FieldPO lookup failed for MVA {mva}: {exc}") from exc

    fpos = [value.strip().upper() for value in fpos if value.strip()]
    if not fpos:
        return "missing"
    return ", ".join(fpos)


def _next_action_cell(row: list[str], next_action_col_1based: int) -> str:
    idx = next_action_col_1based - 1
    if len(row) <= idx:
        return ""
    return row[idx].strip()


def _is_retry_value(value: str) -> bool:
    """Return True when an existing NextAction should be retried."""
    lowered = value.strip().lower()
    return lowered in {"error", "missing"}


def _collect_candidates(
    values: list[list[str]],
    target_inventory_date: date,
) -> tuple[list[tuple[int, str]], int]:
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
        if inv_date != target_inventory_date:
            continue

        candidates.append((row_idx, mva))

    return candidates, next_action_col + 1  # convert to 1-based for update_cell


def _collect_non_blank_verification_rows(
    values: list[list[str]],
    next_action_col_1based: int,
    target_inventory_date: date,
) -> list[tuple[int, str, str]]:
    """Collect target-date replacement rows where NextAction is already non-blank.

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
        if inv_date != target_inventory_date:
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
    target_inventory_date: date,
) -> list[tuple[int, str]]:
    """Collect target-date replacement rows where NextAction is still blank."""
    if not values:
        return []

    headers = values[0]
    mva_col = _find_col(headers, "MVA")
    action_col = _find_col(headers, "Action", "Damage Type", "damage_type")
    inventory_col = _find_col(headers, "Inventory Date", "Arrival Date")
    if mva_col is None or action_col is None or inventory_col is None:
        return []

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
        if inv_date != target_inventory_date:
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
    if lowered == REVIEW_VALUE:
        return REVIEW_VALUE

    return raw.upper()


def _po_set(value: str) -> frozenset[str] | None:
    """Return the PO numbers in a NextAction value, or None if it is not a PO list."""
    if not _PO_LIST_PATTERN.fullmatch(str(value)):
        return None
    return frozenset(part.strip().upper() for part in str(value).split(","))


def _verification_needs_review(existing_value: str, resolved: str) -> bool:
    """True when a sheet PO value is contradicted by FieldPO.

    Only PO-number values are judged; free-text notes (e.g. "need pic") are
    never overwritten. Every sheet PO must still be an eligible FieldPO PO for
    this vehicle; FieldPO may show extra POs added later (e.g. calibration).
    """
    existing_pos = _po_set(existing_value)
    if existing_pos is None:
        return False
    resolved_pos = _po_set(resolved)
    if resolved_pos is None:
        return True
    return not existing_pos <= resolved_pos


def _row_vins(values: list[list[str]]) -> dict[int, str]:
    """Map 1-based sheet row index -> VIN cell value."""
    if not values:
        return {}
    vin_col = _find_col(values[0], "VIN")
    if vin_col is None:
        raise RuntimeError("Required column missing: VIN")
    return {
        row_idx: (row[vin_col].strip() if len(row) > vin_col else "")
        for row_idx, row in enumerate(values[1:], start=2)
    }


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
        "--inventory-date",
        help="Inventory Date to process in YYYY-MM-DD format.",
    )
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


def _parse_inventory_date_arg(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError("--inventory-date must use YYYY-MM-DD format") from exc


def run(target_inventory_date: date | None = None) -> None:
    _setup_logging()
    log.info("Starting FieldPO NextAction fill script")
    args = _parse_args() if target_inventory_date is None else argparse.Namespace(
        inventory_date=None,
        rollback_file=None,
        rollback_latest=False,
    )

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

    if target_inventory_date is None:
        if not args.inventory_date:
            raise RuntimeError("--inventory-date is required for FieldPO processing")
        target_inventory_date = _parse_inventory_date_arg(args.inventory_date)

    values = ws.get_all_values()
    candidates, next_action_col = _collect_candidates(values, target_inventory_date)
    verify_rows = _collect_non_blank_verification_rows(values, next_action_col, target_inventory_date)
    log.info(
        "Found %d lookup candidate row(s) and %d non-blank verification row(s) for Inventory Date %s.",
        len(candidates),
        len(verify_rows),
        target_inventory_date.isoformat(),
    )
    if not candidates and not verify_rows:
        log.info("No eligible rows to update or verify. Exiting.")
        return

    # Verification can overwrite a contradicted PO with "review", so back up
    # those rows as well as the lookup candidates.
    backup_rows = candidates + [(row_idx, mva) for row_idx, mva, _ in verify_rows]
    backup_path = _write_backup(values, backup_rows, next_action_col, spreadsheet_id, sheet_name)
    log.info("Backup written: %s", backup_path)

    FIELDPO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    row_vins = _row_vins(values)
    cache: dict[tuple[str, str], str] = {}
    updated = 0
    flagged_review = 0
    review_count = 0
    missing = 0
    processed_rows: list[int] = [row_idx for row_idx, _ in candidates]
    verify_match = 0
    verify_mismatch = 0

    def _record_value(value: str) -> None:
        nonlocal updated, review_count, missing
        updated += 1
        if value == REVIEW_VALUE:
            review_count += 1
        elif value == "missing":
            missing += 1

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(FIELDPO_PROFILE_DIR), headless=False)
        try:
            page = context.new_page()
            _ensure_fieldpo_login(page)

            # Single pass: process all candidate rows from the initial snapshot.
            for row_idx, mva in candidates:
                key = (mva, row_vins.get(row_idx, ""))
                if key in cache:
                    result = cache[key]
                else:
                    log.info("Lookup MVA %s", mva)
                    result = _resolve_next_action(page, mva, key[1], target_inventory_date)
                    cache[key] = result

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
                key = (mva, row_vins.get(row_idx, ""))
                if key in cache:
                    resolved = cache[key]
                else:
                    log.info("Verify lookup MVA %s", mva)
                    resolved = _resolve_next_action(page, mva, key[1], target_inventory_date)
                    cache[key] = resolved

                expected = _normalize_next_action_for_compare(existing_value)
                actual = _normalize_next_action_for_compare(resolved)

                if _verification_needs_review(existing_value, resolved):
                    verify_mismatch += 1
                    flagged_review += 1
                    ws.update_cell(row_idx, next_action_col, REVIEW_VALUE)
                    log.warning(
                        "Verify row %d MVA %s mismatch: sheet=%s fieldpo=%s. Wrote review.",
                        row_idx,
                        mva,
                        existing_value,
                        resolved,
                    )
                elif expected == actual or _po_set(existing_value) is not None:
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
    remaining_blanks = _collect_in_scope_blank_rows(
        remaining_values,
        next_action_col,
        target_inventory_date,
    )
    forced_scope_fills = 0
    for row_idx, mva in remaining_blanks:
        ws.update_cell(row_idx, next_action_col, "missing")
        forced_scope_fills += 1
        log.warning("Final guard patched row %d MVA %s to missing.", row_idx, mva)

    log.info(
        "Complete. Updated=%d, review=%d, missing=%d, po=%d, patched_blanks=%d, forced_scope_fills=%d, verify_match=%d, verify_mismatch=%d, verify_flagged_review=%d",
        updated,
        review_count,
        missing,
        updated - review_count - missing,
        patched_blanks,
        forced_scope_fills,
        verify_match,
        verify_mismatch,
        flagged_review,
    )


if __name__ == "__main__":
    run()
