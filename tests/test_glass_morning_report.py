from datetime import date

import pytest

from glass_morning_report import (
    REPORT_COLUMNS, ROOT, _default_output_path, build_rows, build_sections,
    generate_report, latest_inventory_date, read_sheet_rows, render_report,
)


HEADERS = [
    "Inventory Date", "Original Date", "MVA", "Next Action", "VIN", "Make",
    "Location", "Action", "Area", "Claim#", "WorkItemCreated",
]


def test_build_rows_includes_only_today_and_excludes_blank_vin_rows() -> None:
    values = [
        HEADERS,
        ["09/23/2026", "09/20/2026", "11111111", "FPO1", "VIN1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
        ["09/22/2026", "09/22/2026", "22222222", "FPO2", "VIN2", "Honda", "APO", "Repair(SuperGlass)", "Windshield", "Listed", ""],
        ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"],
    ]
    rows = build_rows(values, today=date(2026, 9, 23))
    assert [row.mva for row in rows] == ["11111111"]
    assert rows[0].age == 3


def test_read_sheet_rows_uses_windows_trust_google_client(monkeypatch, tmp_path) -> None:
    service_path = tmp_path / "service-account.json"
    service_path.write_text("{}", encoding="utf-8")
    values = [["Inventory Date"], ["10/13/2026"]]
    worksheet = type("Worksheet", (), {"get_all_values": lambda self: values})()
    spreadsheet = type("Spreadsheet", (), {"worksheet": lambda self, name: worksheet})()
    client = type("Client", (), {"open_by_key": lambda self, key: spreadsheet})()
    used_paths = []
    monkeypatch.setattr(
        "glass_morning_report._google_sheets_client",
        lambda path: used_paths.append(path) or client,
    )

    result = read_sheet_rows({
        "spreadsheet_id": "test-spreadsheet",
        "service_account_json": str(service_path),
    })

    assert used_paths == [str(service_path)]
    assert result == values


def test_generate_report_uses_most_recent_inventory_date_including_future(monkeypatch, tmp_path) -> None:
    values = [
        HEADERS,
        ["10/03/2026", "10/02/2026", "11111111", "FPO1", "VIN1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
        ["10/08/2026", "10/08/2026", "22222222", "FPO2", "VIN2", "Honda", "APO", "Repair(SuperGlass)", "Windshield", "Listed", ""],
        ["10/06/2026", "10/05/2026", "33333333", "FPO3", "VIN3", "Ford", "BB", "Replace(AGN)", "Windshield", "Missing", ""],
    ]
    monkeypatch.setattr("glass_morning_report.read_sheet_rows", lambda config: values)
    monkeypatch.setattr("glass_morning_report.REPORTS_DIR", tmp_path)

    output_path = generate_report(open_browser=False)

    assert latest_inventory_date(values) == date(2026, 10, 8)
    assert output_path.name == "glass_morning_report_2026-10-08.html"
    report = output_path.read_text(encoding="utf-8")
    assert "MVA 22222222" in report
    assert "MVA 11111111" not in report
    assert "New on report date" in report


def test_build_rows_requires_canonical_headers_but_not_optional_ones() -> None:
    with pytest.raises(RuntimeError, match="Claim#"):
        build_rows([["Inventory Date", "Original Date", "MVA", "VIN", "Make", "Location", "Action", "Area"]], today=date(2026, 9, 23))


def test_build_rows_generates_report_without_optional_enrichment_columns() -> None:
    canonical_headers = [
        "Inventory Date", "Original Date", "MVA", "VIN", "Make",
        "Location", "Action", "Area", "Claim#",
    ]
    values = [
        canonical_headers,
        ["09/23/2026", "09/23/2026", "11111111", "VIN1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing"],
    ]
    rows = build_rows(values, today=date(2026, 9, 23))
    assert len(rows) == 1
    assert rows[0].next_action == ""
    assert rows[0].work_item == ""


def test_stale_metric_includes_all_report_rows_regardless_of_vendor() -> None:
    values = [
        HEADERS,
        ["09/23/2026", "09/18/2026", "11111111", "FPO1", "VIN1", "Toyota", "BB", "Replace(AVIS)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/19/2026", "22222222", "FPO2", "VIN2", "Honda", "BB", "Replace(AVIS)", "Windshield", "Missing", ""],
        ["09/23/2026", "08/30/2026", "33333333", "FPO3", "VIN3", "Ford", "BB", "Replace(AGN)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/17/2026", "44444444", "FPO4", "VIN4", "Mazda", "BB", "Repair(SuperGlass)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/20/2026", "55555555", "FPO5", "VIN5", "Kia", "BB", "Replace(AGN)", "Windshield", "Missing", ""],
    ]
    output = render_report(build_rows(values, today=date(2026, 9, 23)), today=date(2026, 9, 23))
    metric_cell = output.split("Units 5+ days old</div>")[1].split("</div></td>")[0]
    assert "MVA 11111111" in metric_cell
    assert "MVA 22222222" not in metric_cell
    assert "MVA 33333333" in metric_cell
    assert "MVA 44444444" in metric_cell
    assert "MVA 55555555" not in metric_cell


def test_sections_follow_approved_order_and_oldest_first() -> None:
    values = [
        HEADERS,
        ["09/23/2026", "09/22/2026", "11111111", "FPO1", "VIN1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
        ["09/23/2026", "09/10/2026", "22222222", "FPO2", "VIN2", "Honda", "BB", "Replace(AGN)", "Windshield", "Listed", "Y"],
        ["09/23/2026", "09/20/2026", "33333333", "", "VIN3", "Ford", "APO", "Repair(SuperGlass)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/01/2026", "44444444", "", "VIN4", "Buick", "APO", "Replace(AVIS)", "Sunroof", "Listed", "Y"],
        ["09/23/2026", "09/21/2026", "55555555", "Needs photo", "VIN5", "Kia", "Augusta", "Replace(AGN)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/19/2026", "66666666", "", "VIN6", "Subaru", "", "Replace(AGN)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/15/2026", "77777777", "", "VIN7", "Mazda", "Augusta", "Warranty", "Warranty", "Missing", ""],
    ]
    sections = build_sections(build_rows(values, today=date(2026, 9, 23)))
    assert [section.title for section in sections] == [
        "AGN (Replacements)", "Super Glass (Repairs)", "AVIS (TBK)", "Local Market",
    ]
    assert [row.mva for row in sections[0].rows] == ["22222222", "77777777", "11111111"]
    assert [row.mva for row in sections[-1].rows] == ["66666666", "55555555"]


def test_render_report_is_copy_ready_and_escapes_sheet_data() -> None:
    long_fpo = "FPO1165261/FPO1166727/FPO1168890"
    values = [
        HEADERS,
        ["09/23/2026", "09/01/2026", "11111111", "Needs photo", "VIN<1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
        ["09/23/2026", "09/01/2026", "22222222", long_fpo, "VIN2", "Honda", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
    ]
    output = render_report(build_rows(values, today=date(2026, 9, 23)), today=date(2026, 9, 23))
    assert "Copy report for Outlook" in output
    assert '<table id="report" role="presentation" width="1100" cellpadding=' in output
    assert '<table id="report" role="presentation" width="1100" align=' not in output
    assert "border:2px solid #111;" in output
    assert "AGN (Replacements)" in output
    assert "NEEDS PHOTO" in output
    assert "22d" in output
    assert "VIN&lt;1" in output
    assert "VIN<1" not in output
    assert long_fpo in output
    assert "white-space:normal;word-wrap:break-word;overflow-wrap:break-word;" in output
    assert '<col width="200" style="width:200px;">' in output
    assert '<col width="137" style="width:137px;">' in output
    assert 'name="viewport"' in output
    assert 'class="report-stage"' in output
    assert 'class="mobile-report"' in output
    assert 'class="mobile-unit"' in output
    assert "mobile-vin" in output
    assert "function fitReport()" in output
    assert "available/1100" in output


def test_default_output_uses_root_reports_folder_and_report_date() -> None:
    assert _default_output_path(date(2026, 9, 23)) == ROOT / "reports" / "glass_morning_report_2026-09-23.html"


def test_column_layout_prioritizes_vin_without_exceeding_report_width() -> None:
    widths = dict(REPORT_COLUMNS)
    assert widths["Inv"] == 44
    assert widths["Orig"] == 44
    assert widths["Next action"] == 200
    assert widths["VIN"] == 137
    assert sum(widths.values()) == 1050