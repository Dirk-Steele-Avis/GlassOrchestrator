from datetime import date

import pytest

from glass_morning_report import REPORT_COLUMNS, ROOT, _default_output_path, build_rows, build_sections, render_report


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


def test_build_rows_requires_live_report_headers() -> None:
    with pytest.raises(RuntimeError, match="Next Action, WorkItemCreated"):
        build_rows([["Inventory Date", "Original Date", "MVA", "VIN", "Make", "Location", "Action", "Area", "Claim#"]], today=date(2026, 9, 23))


def test_sections_follow_approved_order_and_oldest_first() -> None:
    values = [
        HEADERS,
        ["09/23/2026", "09/22/2026", "11111111", "FPO1", "VIN1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"],
        ["09/23/2026", "09/10/2026", "22222222", "FPO2", "VIN2", "Honda", "BB", "Replace(AGN)", "Windshield", "Listed", "Y"],
        ["09/23/2026", "09/20/2026", "33333333", "", "VIN3", "Ford", "APO", "Repair(SuperGlass)", "Windshield", "Missing", ""],
        ["09/23/2026", "09/01/2026", "44444444", "", "VIN4", "Buick", "APO", "Replace(AVIS)", "Sunroof", "Listed", "Y"],
        ["09/23/2026", "09/21/2026", "55555555", "Needs photo", "VIN5", "Kia", "Augusta", "Replace(AGN)", "Windshield", "Missing", ""],
    ]
    sections = build_sections(build_rows(values, today=date(2026, 9, 23)))
    assert [section.title for section in sections] == [
        "AGN (Replacements)", "Super Glass (Repairs)", "AVIS (TBK)", "Local Market",
    ]
    assert [row.mva for row in sections[0].rows] == ["22222222", "11111111"]


def test_render_report_is_copy_ready_and_escapes_sheet_data() -> None:
    values = [HEADERS, ["09/23/2026", "09/01/2026", "11111111", "Needs photo", "VIN<1", "Toyota", "BB", "Replace(AGN)", "Windshield", "Missing", "Y"]]
    output = render_report(build_rows(values, today=date(2026, 9, 23)), today=date(2026, 9, 23))
    assert "Copy report for Outlook" in output
    assert "AGN (Replacements)" in output
    assert "NEEDS PHOTO" in output
    assert "22d" in output
    assert "VIN&lt;1" in output
    assert "VIN<1" not in output
    assert '<col width="221" style="width:221px;">' in output
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
    assert widths["VIN"] == 221
    assert widths["VIN"] > widths["Make"] + widths["Location"]
    assert sum(widths.values()) == 1050