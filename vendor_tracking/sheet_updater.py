"""AutoGlassNow vendor tracking — Google Sheet updater.

Connects to the GlassClaims worksheet and updates vendor lifecycle
columns for rows matched by the VIN + Inventory Date compound key.

Match rules:
  - Exactly 1 match  →  update allowed
  - 0 matches        →  write Needs Review note; include in summary
  - 2+ matches       →  write Needs Review note; include in summary

All writes are targeted: only the named vendor tracking columns are
touched. Existing data in the primary pipeline columns is never modified.

New vendor tracking columns are appended to the header row on first use
if they are not already present.
"""

import logging
import re
from datetime import datetime
from typing import Any, Optional

try:
    import gspread  # pyright: ignore[reportMissingImports]
    from gspread.utils import rowcol_to_a1  # pyright: ignore[reportMissingImports]
    _HAS_GSPREAD = True
except ModuleNotFoundError:
    gspread = None  # type: ignore[assignment]
    _HAS_GSPREAD = False

log = logging.getLogger("vendor_tracking.sheet_updater")

# Vendor tracking columns managed by this module (Phase 3 subset).
# Phase 4 will add Completed Date and Turnaround Days.
VENDOR_TRACKING_COLUMNS = [
    "Repair Status",
    "Appointment Date",
    "Cost",
    "Approval Needed",
    "Repair Status Notes",
    "Vendor Job Number",
    # Phase 4:
    # "Completed Date",
    # "Turnaround Days",
]

# Repair Status values
STATUS_SCHEDULED = "Scheduled"
STATUS_TECHNICIAN_ASSIGNED = "Technician Assigned"
STATUS_APPROVAL_NEEDED = "Approval Needed"
STATUS_COMPLETED = "Completed"
STATUS_NEEDS_REVIEW = "Needs Review"
RESOLVED_STATUSES = {"completed", "resolved", "closed"}

# Precedence order: higher index wins. Completed is terminal.
# Needs Review is the lowest real-status rank so any subsequent vendor
# status update (Approval Needed, Technician Assigned, etc.) can overwrite it.
_STATUS_PRECEDENCE = [
    STATUS_NEEDS_REVIEW,
    STATUS_SCHEDULED,
    STATUS_TECHNICIAN_ASSIGNED,
    STATUS_APPROVAL_NEEDED,
    STATUS_COMPLETED,
]


def _status_rank(status: str) -> int:
    try:
        return _STATUS_PRECEDENCE.index(status)
    except ValueError:
        return -1


def normalize_vin_for_match(raw: str) -> str:
    """Uppercase, strip non-alphanumeric, require 17 chars. Returns '' if invalid."""
    normalized = re.sub(r"[^A-Z0-9]", "", raw.upper())
    return normalized if len(normalized) == 17 else ""


def normalize_date_for_match(raw: str) -> str:
    """Normalize a date string to YYYY-MM-DD for stable comparison."""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


class MatchResult:
    """Outcome of a VIN + date sheet row lookup."""

    def __init__(self, row_index: Optional[int], status: str, note: str = "") -> None:
        # 1-based Google Sheets row index (includes header row); None if unresolved.
        self.row_index = row_index
        # "ok", "not_found", "ambiguous"
        self.status = status
        self.note = note

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


class VendorSheetUpdater:
    """Manages vendor lifecycle column updates on the GlassClaims worksheet."""

    def __init__(self, spreadsheet_id: str, sheet_name: str, service_account_json: str) -> None:
        if not _HAS_GSPREAD:
            raise ModuleNotFoundError(
                "Missing dependency 'gspread'. Run the project venv: "
                "'.venv\\Scripts\\python.exe -m pip install gspread'"
            )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name
        self._service_account_json = service_account_json
        self._ws: Any = None  # gspread Worksheet
        self._headers: list[str] = []
        self._all_values: list[list[str]] = []

    # ─── Connection ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Authenticate and open the target worksheet."""
        try:
            gc = gspread.service_account(filename=self._service_account_json)
            sh = gc.open_by_key(self._spreadsheet_id)
            self._ws = sh.worksheet(self._sheet_name)
        except PermissionError as exc:
            raise RuntimeError(
                "Google Sheets access denied (403). Share the target spreadsheet with the "
                "service account in Service_account.json, or verify the spreadsheet ID is correct."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Could not open worksheet '{self._sheet_name}' in spreadsheet '{self._spreadsheet_id}': {type(exc).__name__}: {exc}"
            ) from exc
        log.info("Connected to sheet '%s' (%s)", self._sheet_name, self._spreadsheet_id)
        self._refresh_cache()

    def _refresh_cache(self) -> None:
        """Reload all sheet values and header index into memory."""
        self._all_values = self._ws.get_all_values()
        self._headers = self._all_values[0] if self._all_values else []

    # ─── Column management ────────────────────────────────────────────────────

    def ensure_columns(self, column_names: Optional[list[str]] = None) -> None:
        """Append any missing vendor tracking columns to the header row.

        Skips columns that already exist (case-insensitive match).
        """
        if column_names is None:
            column_names = VENDOR_TRACKING_COLUMNS

        existing_lower = {h.strip().lower() for h in self._headers}
        missing = [c for c in column_names if c.lower() not in existing_lower]
        if not missing:
            log.debug("All vendor tracking columns already present.")
            return

        # Append missing headers to the right of the existing header row.
        next_col = len(self._headers) + 1
        for col_name in missing:
            self._ws.update_cell(1, next_col, col_name)
            log.info("Added column '%s' at position %d", col_name, next_col)
            next_col += 1

        self._refresh_cache()

    def _col_index(self, column_name: str) -> Optional[int]:
        """Return 1-based column index for a header name, or None if absent."""
        lower = column_name.lower()
        for idx, header in enumerate(self._headers):
            if header.strip().lower() == lower:
                return idx + 1  # 1-based
        return None

    # ─── Row matching ────────────────────────────────────────────────────────

    def find_row(self, vin: str, arrival_date: str) -> MatchResult:
        """Find the sheet row matching VIN + Inventory Date compound key.

        Returns a MatchResult describing the outcome.
        """
        norm_vin = normalize_vin_for_match(vin)
        norm_date = normalize_date_for_match(arrival_date)

        if not norm_vin:
            return MatchResult(None, "not_found", f"Invalid VIN: '{vin}'")

        vin_col = self._col_index("VIN")
        date_col = self._col_index("Inventory Date")
        if date_col is None:
            date_col = self._col_index("Arrival Date")

        if vin_col is None or date_col is None:
            return MatchResult(
                None, "not_found",
                "Sheet missing required column(s): VIN or Inventory/Arrival Date"
            )

        matching_rows: list[int] = []
        for row_idx, row in enumerate(self._all_values[1:], start=2):  # skip header; 1-based
            if len(row) < max(vin_col, date_col):
                continue
            sheet_vin = normalize_vin_for_match(row[vin_col - 1])
            sheet_date = normalize_date_for_match(row[date_col - 1])
            if sheet_vin == norm_vin and sheet_date == norm_date:
                matching_rows.append(row_idx)

        if len(matching_rows) == 1:
            return MatchResult(matching_rows[0], "ok")
        if len(matching_rows) == 0:
            return MatchResult(None, "not_found", f"VIN {norm_vin} + {norm_date} not found")
        return MatchResult(
            None, "ambiguous",
            f"VIN {norm_vin} + {norm_date} matched {len(matching_rows)} rows"
        )

    def find_unique_row_by_vin(self, vin: str) -> MatchResult:
        """Find exactly one sheet row by VIN when an event has no arrival date."""
        norm_vin = normalize_vin_for_match(vin)
        if not norm_vin:
            return MatchResult(None, "not_found", f"Invalid VIN: '{vin}'")

        vin_col = self._col_index("VIN")
        if vin_col is None:
            return MatchResult(None, "not_found", "Sheet missing required column: VIN")

        matching_rows = [
            row_idx
            for row_idx, row in enumerate(self._all_values[1:], start=2)
            if len(row) >= vin_col and normalize_vin_for_match(row[vin_col - 1]) == norm_vin
        ]
        if len(matching_rows) == 1:
            return MatchResult(matching_rows[0], "ok")
        if not matching_rows:
            return MatchResult(None, "not_found", f"VIN {norm_vin} not found")
        return MatchResult(None, "ambiguous", f"VIN {norm_vin} matched {len(matching_rows)} rows")

    def get_row_fields(self, row_index: int, column_names: list[str]) -> dict[str, str]:
        """Return named values from one cached sheet row."""
        if row_index < 2 or row_index > len(self._all_values):
            return {}

        row = self._all_values[row_index - 1]
        values: dict[str, str] = {}
        for column_name in column_names:
            col_index = self._col_index(column_name)
            if col_index is not None:
                values[column_name] = row[col_index - 1].strip() if len(row) >= col_index else ""
        return values

    def has_unique_resolved_vin(self, vin: str) -> bool:
        """Return True when VIN maps to exactly one row and that row is resolved."""
        norm_vin = normalize_vin_for_match(vin)
        if not norm_vin:
            return False

        vin_col = self._col_index("VIN")
        if vin_col is None:
            return False

        matching_rows = [
            row_idx
            for row_idx, row in enumerate(self._all_values[1:], start=2)
            if len(row) >= vin_col and normalize_vin_for_match(row[vin_col - 1]) == norm_vin
        ]
        if len(matching_rows) != 1:
            return False

        return self.is_row_resolved(matching_rows[0])

    def is_row_resolved(self, row_index: int) -> bool:
        """Return True if the row's Repair Status indicates a resolved lifecycle state."""
        repair_status_col = self._col_index("Repair Status")
        if repair_status_col is None:
            return False

        if row_index < 1 or row_index > len(self._all_values):
            return False

        row_data = self._all_values[row_index - 1]
        if len(row_data) < repair_status_col:
            return False

        current_status = row_data[repair_status_col - 1].strip().lower()
        return current_status in RESOLVED_STATUSES

    # ─── Row updates ─────────────────────────────────────────────────────────

    def update_vendor_fields(self, row_index: int, fields: dict[str, Any]) -> None:
        """Write named vendor tracking column values for the given row.

        Only columns present in the sheet header will be written.
        Status precedence is enforced for the Repair Status column:
        a lower-precedence status cannot overwrite a higher one.

        Args:
            row_index: 1-based Google Sheets row number.
            fields:    Dict of {column_name: value} to write.
        """
        # Enforce Repair Status precedence before writing
        if row_index < 1 or row_index > len(self._all_values):
            log.error(
                "update_vendor_fields: row_index %d out of range (sheet has %d rows) — skipping write",
                row_index, len(self._all_values),
            )
            return

        if "Repair Status" in fields:
            repair_status_col = self._col_index("Repair Status")
            if repair_status_col is not None:
                row_data = self._all_values[row_index - 1]
                current_status = row_data[repair_status_col - 1].strip() if len(row_data) >= repair_status_col else ""
                incoming_status = str(fields["Repair Status"])
                if _status_rank(incoming_status) < _status_rank(current_status):
                    log.info(
                        "Row %d: Skipping status downgrade '%s' → '%s'",
                        row_index, current_status, incoming_status
                    )
                    fields = {k: v for k, v in fields.items() if k != "Repair Status"}

        pending_updates: list[tuple[int, str, str]] = []
        for col_name, value in fields.items():
            col_idx = self._col_index(col_name)
            if col_idx is None:
                log.warning("Column '%s' not found in sheet — skipping", col_name)
                continue
            serialized_value = str(value) if value is not None else ""
            pending_updates.append((col_idx, col_name, serialized_value))

        if not pending_updates:
            return

        self._ws.batch_update([
            {
                "range": rowcol_to_a1(row_index, col_idx),
                "values": [[serialized_value]],
            }
            for col_idx, _col_name, serialized_value in pending_updates
        ])

        for col_idx, col_name, serialized_value in pending_updates:
            row_data = self._all_values[row_index - 1]
            if len(row_data) < col_idx:
                row_data.extend([""] * (col_idx - len(row_data)))
            row_data[col_idx - 1] = serialized_value
            log.debug("Row %d col '%s' ← '%s'", row_index, col_name, serialized_value)

    def write_needs_review(self, vin: str, note: str) -> None:
        """Attempt a best-effort Repair Status Notes write when a row can be found by VIN only.

        If the VIN matches exactly one row (regardless of date), writes the note
        there. Otherwise, logs only — no write is made.

        This is a fallback path; callers should prefer find_row() for normal updates.
        """
        norm_vin = normalize_vin_for_match(vin)
        if not norm_vin:
            log.warning("write_needs_review: invalid VIN '%s'", vin)
            return

        vin_col = self._col_index("VIN")
        if vin_col is None:
            return

        matching_rows = [
            row_idx + 2
            for row_idx, row in enumerate(self._all_values[1:])
            if len(row) >= vin_col and normalize_vin_for_match(row[vin_col - 1]) == norm_vin
        ]

        if len(matching_rows) == 1:
            if self.is_row_resolved(matching_rows[0]):
                log.info(
                    "Skipping needs-review write for VIN %s — row %d already resolved",
                    vin,
                    matching_rows[0],
                )
                return

            self.update_vendor_fields(matching_rows[0], {
                "Repair Status": STATUS_NEEDS_REVIEW,
                "Repair Status Notes": note,
            })
