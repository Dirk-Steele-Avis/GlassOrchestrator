"""Import distant-location glass orders into the GlassClaims worksheet."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
from vendor_tracking.sheet_updater import _google_sheets_client


ROOT = Path(__file__).resolve().parent
CONFIG_PATHS = (
    ROOT / "orchestrator_config.json",
    ROOT / "orchestrator_project.json",
    ROOT / "orchestrator_project.local.json",
    ROOT / "orchestrator_config.local.json",
    ROOT / "config" / "config.local.json",
)
SOURCE_HEADERS = (
    "Inventory Date", "MVA", "Location", "Area", "DamageType",
    "GlassSource", "ClaimStatus",
)
SHEET_HEADERS = (
    "Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make",
    "Location", "Action", "Area", "Claim#", "WorkItem",
)
REQUIRED_SHEET_HEADERS = (
    "Inventory Date", "Original Date", "MVA", "VIN", "Make", "Location",
    "Action", "Area", "Claim#",
)
SUMMARY_MARKERS = {1: "Avg Repair Days", 7: "Repairs", 9: "Claims", 10: "Total"}
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d")


@dataclass(frozen=True)
class ImportRecord:
    inventory_date: date
    mva: str
    location: str
    action: str
    area: str
    claim: str

    @property
    def key(self) -> tuple[str, str]:
        return self.inventory_date.isoformat(), self.mva


def _load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in CONFIG_PATHS:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to load configuration {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Configuration must be a JSON object: {path}")
        config.update(value)
    return config


def _normalize_mva(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("MVA must be an 8- or 9-digit value")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"MVA must be an integer, got {value!r}")
        raw = str(int(value))
    else:
        raw = str(value).strip()
    if not re.fullmatch(r"\d{8,9}", raw):
        raise ValueError(f"MVA must be an 8- or 9-digit value, got {value!r}")
    normalized = raw.zfill(9)
    if not normalized.startswith("0"):
        raise ValueError(f"MVA must be 9 digits with the leading zero, got {value!r}")
    return normalized


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(raw, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Inventory Date must use MM/DD/YYYY or YYYY-MM-DD, got {value!r}")


def _source_records(
    headers: list[object], source_rows: Iterable[Iterable[object]], *,
    areas: dict[str, str], area_aliases: dict[str, str],
) -> list[ImportRecord]:
    names = [str(value or "").strip() for value in headers]
    while names and not names[-1]:
        names.pop()
    if len(names) != len(set(names)):
        raise RuntimeError("Source file contains duplicate column headers")
    missing = [name for name in SOURCE_HEADERS if name not in names]
    extra = [name for name in names if name not in SOURCE_HEADERS]
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise RuntimeError(f"Source headers do not match the import contract ({'; '.join(details)})")

    indexes = {name: names.index(name) for name in SOURCE_HEADERS}
    records: list[ImportRecord] = []
    seen: set[tuple[str, str]] = set()
    for row_number, source in enumerate(source_rows, start=2):
        values = list(source)
        if not any(str(value or "").strip() for value in values):
            continue
        values += [None] * max(0, len(names) - len(values))
        try:
            inventory_date = _parse_date(values[indexes["Inventory Date"]])
            mva = _normalize_mva(values[indexes["MVA"]])
            location = str(values[indexes["Location"]] or "").strip()
            if not location:
                raise ValueError("Location is required")

            area_code = str(values[indexes["Area"]] or "").strip().upper()
            area_code = area_aliases.get(area_code, area_code)
            area = areas.get(area_code)
            if not area:
                raise ValueError(f"Unknown glass area code {area_code!r}")

            damage_type = str(values[indexes["DamageType"]] or "").strip().casefold()
            source_name = re.sub(r"\s+", "", str(values[indexes["GlassSource"]] or "")).upper()
            action_map = {
                ("replacement", "AGN"): "Replace(AGN)",
                ("repair", "AGN"): "Repair(AGN)",
                ("repair", "SUPERGLASS"): "Repair(SuperGlass)",
            }
            action = action_map.get((damage_type, source_name))
            if action is None:
                raise ValueError(
                    f"Unsupported DamageType/GlassSource pair: "
                    f"{values[indexes['DamageType']]!r}/{values[indexes['GlassSource']]!r}"
                )

            claim_status = str(values[indexes["ClaimStatus"]] or "").strip().casefold()
            claim_map = {"listed": "Listed", "missing": "Missing"}
            claim = claim_map.get(claim_status)
            if claim is None:
                raise ValueError(f"ClaimStatus must be listed or missing, got {claim_status!r}")
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Invalid source row {row_number}: {exc}") from exc

        record = ImportRecord(inventory_date, mva, location, action, area, claim)
        if record.key in seen:
            raise RuntimeError(
                f"Duplicate source order for Inventory Date {inventory_date:%m/%d/%Y}, MVA {mva}"
            )
        seen.add(record.key)
        records.append(record)

    if not records:
        raise RuntimeError("Source file contains no order rows")
    return records


def read_source_file(
    path: Path, *, worksheet_name: str | None = None,
    areas: dict[str, str], area_aliases: dict[str, str],
) -> list[ImportRecord]:
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as source_file:
            rows = list(csv.reader(source_file))
        if not rows:
            raise RuntimeError("CSV source file is empty")
        return _source_records(rows[0], rows[1:], areas=areas, area_aliases=area_aliases)
    if path.suffix.casefold() != ".xlsx":
        raise RuntimeError("Source file must be .xlsx or .csv")

    workbook = load_workbook(path, data_only=True, read_only=True)
    if worksheet_name is None:
        if len(workbook.sheetnames) != 1:
            raise RuntimeError(
                "Workbook has multiple worksheets; specify one with --worksheet"
            )
        worksheet_name = workbook.sheetnames[0]
    if worksheet_name not in workbook.sheetnames:
        raise RuntimeError(f"Worksheet not found: {worksheet_name}")
    try:
        worksheet = workbook[worksheet_name]
        rows = worksheet.iter_rows(values_only=True)
        try:
            headers = list(next(rows))
        except StopIteration as exc:
            raise RuntimeError(f"Worksheet {worksheet_name!r} is empty") from exc
        return _source_records(headers, rows, areas=areas, area_aliases=area_aliases)
    finally:
        workbook.close()


def _summary_row(all_values: list[list[str]]) -> int:
    matches = [
        row_number
        for row_number, row in enumerate(all_values[1:], start=2)
        if all(len(row) > index and row[index].strip() == marker
               for index, marker in SUMMARY_MARKERS.items())
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"GlassClaims summary boundary must occur exactly once; found {len(matches)}"
        )
    return matches[0]


def _existing_keys(
    all_values: list[list[str]], candidate_dates: set[str] | None = None,
) -> set[tuple[str, str]]:
    if not all_values:
        raise RuntimeError("GlassClaims is empty; expected a header row")
    headers = [str(value or "").strip() for value in all_values[0]]
    named_headers = [header for header in headers if header]
    if len(named_headers) != len(set(named_headers)):
        raise RuntimeError("GlassClaims contains duplicate non-empty column headers")
    missing = [name for name in REQUIRED_SHEET_HEADERS if name not in headers]
    if missing:
        raise RuntimeError(
            f"GlassClaims is missing required column(s): {', '.join(missing)}"
        )
    summary_row = _summary_row(all_values)
    mva_index = headers.index("MVA")
    date_index = headers.index("Inventory Date")
    keys: set[tuple[str, str]] = set()
    for row_number, row in enumerate(all_values[1:summary_row - 1], start=2):
        if len(row) <= max(mva_index, date_index) or not row[mva_index].strip():
            continue
        try:
            inventory_date = _parse_date(row[date_index]).isoformat()
        except ValueError:
            continue
        if candidate_dates is not None and inventory_date not in candidate_dates:
            continue
        try:
            key = (inventory_date, _normalize_mva(row[mva_index]))
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Invalid existing GlassClaims row {row_number}: {exc}") from exc
        keys.add(key)
    return keys


def _original_dates(
    records: list[ImportRecord], all_values: list[list[str]], incident_window_days: int,
) -> dict[tuple[str, str], date]:
    headers = [str(value or "").strip() for value in all_values[0]]
    mva_index = headers.index("MVA")
    inventory_index = headers.index("Inventory Date")
    original_index = headers.index("Original Date")
    summary_row = _summary_row(all_values)
    history: dict[str, list[tuple[date, date]]] = {}
    for row in all_values[1:summary_row - 1]:
        if len(row) <= max(mva_index, inventory_index, original_index) or not row[mva_index].strip():
            continue
        try:
            mva = _normalize_mva(row[mva_index])
            inventory_date = _parse_date(row[inventory_index])
            original_date = _parse_date(row[original_index]) if row[original_index].strip() else inventory_date
        except ValueError:
            continue
        history.setdefault(mva, []).append((inventory_date, original_date))

    result: dict[tuple[str, str], date] = {}
    for record in records:
        previous_rows = sorted(
            (entry for entry in history.get(record.mva, []) if entry[0] <= record.inventory_date),
            reverse=True,
        )
        episode: list[tuple[date, date]] = []
        previous_date = record.inventory_date
        for inventory_date, original_date in previous_rows:
            if (previous_date - inventory_date).days > incident_window_days:
                break
            episode.append((inventory_date, original_date))
            previous_date = inventory_date
        if not episode:
            result[record.key] = record.inventory_date
            continue

        earliest_inventory = episode[-1][0]
        valid_original_dates = [
            original_date
            for _, original_date in episode
            if 0 <= (earliest_inventory - original_date).days <= incident_window_days
        ]
        result[record.key] = min([earliest_inventory, *valid_original_dates])
    return result


def prepare_sheet_rows(
    records: list[ImportRecord], work_item: str,
    original_dates: dict[tuple[str, str], date] | None = None,
) -> list[list[str]]:
    rows = []
    for record in records:
        inventory_date = record.inventory_date.strftime("%m/%d/%Y")
        original_date = (original_dates or {}).get(record.key, record.inventory_date)
        rows.append([
            inventory_date,
            original_date.strftime("%m/%d/%Y"),
            record.mva[1:],
            "",
            "N/A",
            "",
            record.location,
            record.action,
            record.area,
            record.claim,
            work_item,
        ])
    return rows


def _find_insert_row(all_values: list[list[str]]) -> int:
    if not all_values:
        raise RuntimeError("GlassClaims sheet is empty")
    summary_row = _summary_row(all_values)
    headers = [str(value or "").strip() for value in all_values[0]]
    if "MVA" not in headers:
        raise RuntimeError("GlassClaims is missing required column: MVA")
    mva_index = headers.index("MVA")
    for row_number in range(summary_row - 1, 1, -1):
        row = all_values[row_number - 1]
        if len(row) > mva_index and row[mva_index].strip():
            return row_number + 1
    return 2


def _worksheet(config: dict[str, Any]):
    spreadsheet_id = os.getenv("GLASS_SPREADSHEET_ID", str(config.get("spreadsheet_id", ""))).strip()
    if not spreadsheet_id or spreadsheet_id == "YOUR_SPREADSHEET_ID_HERE":
        raise RuntimeError("GLASS_SPREADSHEET_ID or spreadsheet_id must identify the report workbook")
    service_path = Path(str(config.get("service_account_json", "Service_account.json")))
    service_path = service_path if service_path.is_absolute() else ROOT / service_path
    if not service_path.exists():
        raise RuntimeError(f"Google service account file not found: {service_path}")
    client = _google_sheets_client(str(service_path))
    client.set_timeout(int(config.get("sheets_timeout", 30)))
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(str(config.get("sheet_name", "GlassClaims")))


def _insert_rows(
    worksheet: Any, rows: list[list[str]], insert_row: int, headers: list[str],
) -> None:
    normalized_headers = [str(value or "").strip() for value in headers]
    requests = [{
        "insertDimension": {
            "range": {
                "sheetId": worksheet.id,
                "dimension": "ROWS",
                "startIndex": insert_row - 1,
                "endIndex": insert_row - 1 + len(rows),
            },
            "inheritFromBefore": True,
        }
    }]
    for source_index, header in enumerate(SHEET_HEADERS):
        if header not in normalized_headers:
            continue
        target_index = normalized_headers.index(header)
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": insert_row - 1,
                    "endRowIndex": insert_row - 1 + len(rows),
                    "startColumnIndex": target_index,
                    "endColumnIndex": target_index + 1,
                },
                "rows": [
                    {"values": [{"userEnteredValue": {"stringValue": row[source_index]}}]}
                    for row in rows
                ],
                "fields": "userEnteredValue",
            }
        })
    worksheet.spreadsheet.batch_update({"requests": requests})


def run_import(
    source_path: Path, *, worksheet_name: str | None = None, apply: bool = False,
) -> tuple[list[ImportRecord], int | None]:
    config = _load_config()
    areas = {str(code).upper(): str(label) for code, label in config.get("areas", {}).items()}
    aliases = {
        str(alias).upper(): str(canonical).upper()
        for alias, canonical in config.get("legacy_area_aliases", {}).items()
    }
    records = read_source_file(
        source_path,
        worksheet_name=worksheet_name,
        areas=areas,
        area_aliases=aliases,
    )
    worksheet = _worksheet(config)
    all_values = worksheet.get_all_values()
    candidate_dates = {record.inventory_date.isoformat() for record in records}
    existing = _existing_keys(all_values, candidate_dates)
    collisions = [record for record in records if record.key in existing]
    if collisions:
        joined = ", ".join(
            f"{record.mva} on {record.inventory_date:%m/%d/%Y}" for record in collisions
        )
        raise RuntimeError(f"Import blocked; these MVA/date keys already exist: {joined}")

    rows = prepare_sheet_rows(
        records,
        str(config.get("work_item_default_flag", "verified")),
        _original_dates(
            records,
            all_values,
            int(config.get("incident_window_days", 7)),
        ),
    )
    insert_row = _find_insert_row(all_values)
    if apply:
        _insert_rows(worksheet, rows, insert_row, all_values[0])
    return records, insert_row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Source .xlsx or .csv file")
    parser.add_argument("--worksheet", help="Exact worksheet name when the workbook has multiple sheets")
    parser.add_argument("--apply", action="store_true", help="Insert rows; omit for read-only dry run")
    args = parser.parse_args(argv)
    try:
        records, insert_row = run_import(
            args.input,
            worksheet_name=args.worksheet,
            apply=args.apply,
        )
    except Exception as exc:
        print(f"[ERROR] Distant glass import failed: {exc}", file=sys.stderr)
        return 1
    action = "Inserted" if args.apply else "Dry run: would insert"
    print(f"{action} {len(records)} row(s) at GlassClaims row {insert_row}.")
    for record in records:
        print(
            f"  {record.inventory_date:%m/%d/%Y} | {record.mva} | {record.location} | "
            f"{record.area} | {record.action} | {record.claim}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())