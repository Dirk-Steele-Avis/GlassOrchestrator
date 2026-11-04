from datetime import date, datetime
import csv

import pytest
from openpyxl import Workbook

from import_distant_glass import (
    SOURCE_HEADERS,
    _existing_keys,
    _find_insert_row,
    _original_dates,
    _source_records,
    prepare_sheet_rows,
    read_source_file,
    run_import,
)


AREAS = {"WS": "Windshield", "LFD": "Left Front Door"}
ALIASES = {"FLD": "LFD"}


def _source_rows():
    return [
        [datetime(2026, 10, 14), "056391473", "AUGUSTA GA APO", "ws", "Replacement", "AGN", "listed"],
        [datetime(2026, 10, 14), "060878262", "AUGUSTA GA APO", "ws", "Repair", "AGN", "missing"],
    ]


def test_source_rows_map_to_canonical_sheet_rows_and_preserve_leading_zero() -> None:
    records = _source_records(
        list(SOURCE_HEADERS), _source_rows(), areas=AREAS, area_aliases=ALIASES,
    )

    rows = prepare_sheet_rows(records, "verified")

    assert records[0].mva == "056391473"
    assert rows[0] == [
        "10/14/2026", "10/14/2026", "56391473", "", "N/A", "",
        "AUGUSTA GA APO", "Replace(AGN)", "Windshield", "Listed", "verified",
    ]
    assert rows[1][7:10] == ["Repair(AGN)", "Windshield", "Missing"]


def test_numeric_eight_digit_mva_gets_required_zero_prefix() -> None:
    row = _source_rows()[0]
    row[1] = 56391473

    records = _source_records(
        list(SOURCE_HEADERS), [row], areas=AREAS, area_aliases=ALIASES,
    )

    assert records[0].mva == "056391473"


def test_duplicate_source_mva_and_date_blocks_entire_source() -> None:
    rows = _source_rows()
    rows[1][1] = "056391473"

    with pytest.raises(RuntimeError, match="Duplicate source order"):
        _source_records(list(SOURCE_HEADERS), rows, areas=AREAS, area_aliases=ALIASES)


def test_unknown_area_or_claim_status_fails_with_source_row() -> None:
    row = _source_rows()[0]
    row[3] = "ZZ"

    with pytest.raises(RuntimeError, match="Invalid source row 2: Unknown glass area"):
        _source_records(list(SOURCE_HEADERS), [row], areas=AREAS, area_aliases=ALIASES)

    row = _source_rows()[0]
    row[6] = "verified"
    with pytest.raises(RuntimeError, match="ClaimStatus must be listed or missing"):
        _source_records(list(SOURCE_HEADERS), [row], areas=AREAS, area_aliases=ALIASES)


def test_xlsx_requires_exact_worksheet_when_multiple_exist(tmp_path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Orders"
    worksheet.append(list(SOURCE_HEADERS))
    for row in _source_rows():
        worksheet.append(row)
    workbook.create_sheet("Notes")
    source_path = tmp_path / "orders.xlsx"
    workbook.save(source_path)

    with pytest.raises(RuntimeError, match="specify one with --worksheet"):
        read_source_file(source_path, areas=AREAS, area_aliases=ALIASES)

    records = read_source_file(
        source_path, worksheet_name="Orders", areas=AREAS, area_aliases=ALIASES,
    )
    assert len(records) == 2


def test_csv_source_uses_same_validated_schema(tmp_path) -> None:
    source_path = tmp_path / "orders.csv"
    with source_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(SOURCE_HEADERS)
        writer.writerow(["10/14/2026", "056391473", "AUGUSTA GA APO", "ws", "Repair", "AGN", "missing"])

    records = read_source_file(source_path, areas=AREAS, area_aliases=ALIASES)

    assert records[0].action == "Repair(AGN)"


def _empty_sheet_values():
    return [
        [
            "Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make",
            "Location", "Action", "Area", "Claim#", "WorkItem",
        ],
        ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"],
    ]


def _extended_sheet_values():
    return [
        [
            "Inventory Date", "Original Date", "MVA", "Next Action", "VIN", "Make",
            "Location", "Action", "Area", "Claim#", "WorkItemCreated", "",
            "", "", "", "", "", "", "", "", "Repair Status",
            "Appointment Date", "Cost", "Approval Needed", "Repair Status Notes",
            "Vendor Job Number",
        ],
        ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"],
    ]


def _write_source_xlsx(path, rows=None):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Orders"
    worksheet.append(list(SOURCE_HEADERS))
    for row in rows or _source_rows()[:1]:
        worksheet.append(row)
    workbook.save(path)


def test_run_import_dry_run_reads_but_does_not_write(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "orders.xlsx"
    _write_source_xlsx(source_path)

    class FakeWorksheet:
        def __init__(self):
            self.values = _extended_sheet_values()
            self.writes = []

        def get_all_values(self):
            return self.values

        def batch_update(self, *args, **kwargs):
            self.writes.append((args, kwargs))

    worksheet = FakeWorksheet()
    monkeypatch.setattr("import_distant_glass._load_config", lambda: {
        "areas": AREAS,
        "legacy_area_aliases": ALIASES,
        "work_item_default_flag": "verified",
        "incident_window_days": 7,
    })
    monkeypatch.setattr("import_distant_glass._worksheet", lambda config: worksheet)

    records, insert_row = run_import(source_path, worksheet_name="Orders")

    assert len(records) == 1
    assert insert_row == 2
    assert worksheet.writes == []


def test_run_import_apply_inserts_only_after_duplicate_preflight(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "orders.xlsx"
    _write_source_xlsx(source_path)

    class FakeSpreadsheet:
        def __init__(self):
            self.requests = []

        def batch_update(self, request):
            self.requests.append(request)

    class FakeWorksheet:
        def __init__(self, values):
            self.values = values
            self.spreadsheet = FakeSpreadsheet()
            self.id = 7
            self.writes = []

        def get_all_values(self):
            return self.values

    worksheet = FakeWorksheet(_extended_sheet_values())
    monkeypatch.setattr("import_distant_glass._load_config", lambda: {
        "areas": AREAS,
        "legacy_area_aliases": ALIASES,
        "work_item_default_flag": "verified",
        "incident_window_days": 7,
    })
    monkeypatch.setattr("import_distant_glass._worksheet", lambda config: worksheet)

    records, insert_row = run_import(source_path, worksheet_name="Orders", apply=True)

    assert len(records) == 1
    assert insert_row == 2
    requests = worksheet.spreadsheet.requests[0]["requests"]
    assert requests[0]["insertDimension"]["range"]["startIndex"] == 1
    cells = {request["updateCells"]["range"]["startColumnIndex"]: request for request in requests[1:]}
    assert cells[2]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "56391473"
    }
    assert cells[7]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "Replace(AGN)"
    }
    assert 3 not in cells
    assert 10 not in cells


def test_run_import_blocks_key_already_on_sheet(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "orders.xlsx"
    _write_source_xlsx(source_path)
    values = _extended_sheet_values()
    values.insert(1, ["10/14/2026", "10/14/2026", "56391473", "", "N/A", "", "APO", "Replace(AGN)", "Windshield", "Listed", "verified"])

    class FakeWorksheet:
        def get_all_values(self):
            return values

    monkeypatch.setattr("import_distant_glass._load_config", lambda: {
        "areas": AREAS,
        "legacy_area_aliases": ALIASES,
        "work_item_default_flag": "verified",
        "incident_window_days": 7,
    })
    monkeypatch.setattr("import_distant_glass._worksheet", lambda config: FakeWorksheet())

    with pytest.raises(RuntimeError, match="already exist: 056391473 on 10/14/2026"):
        run_import(source_path, worksheet_name="Orders", apply=True)


def test_existing_sheet_key_normalizes_eight_digit_mva() -> None:
    values = [
        list((
            "Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make",
            "Location", "Action", "Area", "Claim#", "WorkItem",
        )),
        ["10/14/2026", "10/14/2026", "56391473", "", "N/A", "", "APO", "Replace(AGN)", "Windshield", "Listed", "verified"],
        ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"],
    ]

    assert _existing_keys(values, {"2026-10-14"}) == {(date(2026, 10, 14).isoformat(), "056391473")}
    assert _find_insert_row(values) == 3


def test_existing_invalid_mva_blocks_only_on_candidate_inventory_date() -> None:
    values = _extended_sheet_values()
    values.insert(1, ["1/7/2026", "1/7/2026", "5743087", "", "N/A", "", "APO", "Replace(AGN)", "Windshield", "Listed", ""])

    assert _existing_keys(values, {"2026-10-14"}) == set()

    values[1][0] = "10/14/2026"
    with pytest.raises(
        RuntimeError,
        match="Invalid existing GlassClaims row 2: MVA must be an 8- or 9-digit value",
    ):
        _existing_keys(values, {"2026-10-14"})


def test_original_date_carries_forward_within_incident_window() -> None:
    records = _source_records(
        list(SOURCE_HEADERS), _source_rows()[:1], areas=AREAS, area_aliases=ALIASES,
    )
    values = [
        [
            "Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make",
            "Location", "Action", "Area", "Claim#", "WorkItem",
        ],
        ["10/10/2026", "10/08/2026", "056391473", "", "N/A", "", "APO", "Replace(AGN)", "Windshield", "Listed", "verified"],
        ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"],
    ]

    original_dates = _original_dates(records, values, incident_window_days=7)
    rows = prepare_sheet_rows(records, "verified", original_dates)

    assert rows[0][1] == "10/08/2026"