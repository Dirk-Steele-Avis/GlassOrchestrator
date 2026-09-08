# Phase 7 — Compass Work Item Creation
# Standalone script — not part of the main GlassOrchestrator pipeline.
# Reads from GlassClaims sheet, creates Compass work items for eligible MVAs.
#
# Location is read from the sheet when explicitly provided, then from Area.
# If both are blank or missing, it defaults to WINDSHIELD.
# Side/rear damage remains uncommon and may still require operator review.
#
# ARCHITECTURE NOTE: Designed for extraction into the unified automation repo.
# Manifest contract (list of plain dicts) and return contract (summary dict) are portable.
# WorkItemHandler subclasses are the extension point — not this orchestrator.

from utils.logger import log
from core.eligibility import is_notification_eligible, normalize_damage_type
from flows.mva_navigation import warmup_compass, navigate_to_mva
from flows.work_item_flow import check_existing_work_item
from flows.work_item_handler import WorkItemConfig, create_work_item_handler


def read_glass_claims(sheet_client, tab_name: str = "GlassClaims") -> list[dict]:
    """
    Read eligible, unprocessed rows from the GlassClaims sheet tab.

    Filters:
    - is_notification_eligible(row) == True  (Replacement only)
    - WorkItemCreated column is blank

    Returns list of dicts with keys: mva, damage_type, location.
    Location falls back to Area, then WINDSHIELD, when blank.
    """
    rows = sheet_client.get_all_records()
    result = []
    for row in rows:
        if not is_notification_eligible(row):
            continue
        if row.get("WorkItemCreated", "").strip():
            continue
        mva = row.get("MVA")
        if mva is None:
            continue
        mva = str(mva).strip()
        if not mva:
            continue
        result.append({
            "mva": mva,
            "damage_type": normalize_damage_type(row),
            "location": (row.get("Location") or row.get("Area") or "").strip() or "WINDSHIELD",
        })
    return result


class GlassClaimsUpdater:
    """
    Wraps a gspread worksheet to mark WorkItemCreated=Y for a given MVA.
    Implements the sheet_client interface expected by run_glass_work_item_phase().
    Headers and the MVA→row-index map are fetched once and cached to avoid
    O(n²) sheet reads when processing large manifests.
    """

    def __init__(self, worksheet):
        self._ws = worksheet
        self._col_index: int | None = None      # 1-based column index for WorkItemCreated
        self._mva_row: dict[str, int] | None = None  # MVA string → 1-based sheet row

    def _ensure_cache(self) -> None:
        """Populate column index and MVA→row map on first use."""
        if self._col_index is not None:
            return
        headers = self._ws.row_values(1)
        self._col_index = headers.index("WorkItemCreated") + 1  # 1-based
        records = self._ws.get_all_records()
        self._mva_row = {
            str(row.get("MVA", "")).strip(): i
            for i, row in enumerate(records, start=2)  # data starts at row 2
            if str(row.get("MVA", "")).strip()
        }

    def mark_work_item_created(self, mva: str, tab_name: str = "GlassClaims") -> None:
        """Find the row for this MVA and write 'Y' to the WorkItemCreated column."""
        try:
            self._ensure_cache()
            row_index = self._mva_row.get(str(mva).strip())
            if row_index is None:
                log.warning(f"[PHASE7] {mva} - MVA not found in sheet, could not mark WorkItemCreated")
                return
            self._ws.update_cell(row_index, self._col_index, "Y")
            log.info(f"[PHASE7] {mva} - WorkItemCreated marked Y in sheet")
        except Exception as e:
            log.error(f"[PHASE7] {mva} - Failed to mark WorkItemCreated: {e}")


def run_glass_work_item_phase(driver, manifest: list[dict], sheet_client=None,
                               tab_name: str = "GlassClaims") -> dict:
    """
    Process each MVA in the manifest: check for existing work item, create if missing.

    Never aborts the loop — all MVAs are attempted regardless of prior failures.

    Args:
        driver: Selenium WebDriver instance
        manifest: list of dicts with keys mva, damage_type, location
        sheet_client: optional sheet client; if provided, marks WorkItemCreated=Y on success
        tab_name: GlassClaims tab name (passed to sheet_client)

    Returns:
        dict with keys: processed, created, skipped, failed
    """
    summary = {"processed": 0, "created": 0, "skipped": 0, "failed": 0}
    handler = create_work_item_handler("GLASS", driver)

    # Prime the Compass app before processing any real MVAs
    if not warmup_compass(driver):
        log.error("[PHASE7] Compass warm-up failed — aborting run")
        return summary

    for entry in manifest:
        mva = entry["mva"]
        summary["processed"] += 1
        try:
            log.info(f"[PHASE7] {mva} - Starting work item review")

            if not navigate_to_mva(driver, mva):
                log.error(f"[PHASE7] {mva} - Navigation failed, skipping")
                summary["failed"] += 1
                continue

            if check_existing_work_item(driver, mva, work_item_type="GLASS"):
                log.info(f"[PHASE7] {mva} - Open glass work item already exists, skipping")
                summary["skipped"] += 1
                continue

            config = WorkItemConfig(
                mva=mva,
                damage_type=entry.get("damage_type"),
                location=entry.get("location"),
            )
            result = handler.create_work_item(config)

            if result.get("status") == "created":
                log.info(f"[PHASE7] {mva} - Work item created successfully")
                summary["created"] += 1
                if sheet_client is not None:
                    sheet_client.mark_work_item_created(mva, tab_name)
            else:
                log.error(f"[PHASE7] {mva} - Work item creation failed: {result}")
                summary["failed"] += 1

        except Exception as e:
            log.error(f"[PHASE7][ERROR] {mva} - {e}")
            summary["failed"] += 1

    return summary
