import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from invoice_workflow import open_po_analysis as analysis
from invoice_workflow.invoice_parser import InvoicePdfData


PO = "FPO1180184"
VIN = "2C4RC1GG4TR229444"


def orders():
    return [{
        "MVA_NUMBER": "60181505", "VIN_NO": VIN,
        "PO_NUMBER": PO, "PO_STATUS": "OPEN",
        "PO_ESTIMATE_AMOUNT": "340.00", "SUPPLIER_NAME": "AGN GLASS LLC",
    }]


def export(tmp_path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Open Purchase Orders"
    sheet.append(analysis.REQUIRED_COLUMNS)
    for row in rows:
        values = tuple(row) + ("8",) if len(row) == 4 else tuple(row)
        sheet.append(values + ("340.00", "AGN GLASS LLC") if len(values) == 5 else values)
    path = tmp_path / "pos.xlsx"
    workbook.save(path)
    workbook.close()
    return path


def mail(subject="", body="", attachments=()):
    return SimpleNamespace(
        Class=43, Subject=subject, Body=body, EntryID=subject,
        SenderName="AGN GLASS LLC", SenderEmailAddress="noreply@omegaedi.com",
        ReceivedTime="2026-10-21",
        Attachments=SimpleNamespace(
            Count=len(attachments), Item=lambda index: attachments[index - 1],
        ),
    )


def folder(items=(), children=(), name="Inbox"):
    return SimpleNamespace(
        Items=items, Folders=children, FolderPath=name, StoreID="store",
    )


def scan(inbox, rows=None):
    return analysis.scan_inbox(inbox, rows or orders(), logging.getLogger("test"))


def test_load_export_preserves_metadata(tmp_path):
    path = export(tmp_path, [("60181505", VIN, PO, "OPEN")])
    assert analysis.load_open_pos(path) == orders()


def test_age_filter_excludes_exactly_seven_days_and_younger(tmp_path):
    path = export(tmp_path, [
        ("60181505", VIN, "FPO1180184", "OPEN", "7"),
        ("60181505", VIN, "FPO1180183", "OPEN", "0"),
        ("60181505", VIN, "FPO1180182", "OPEN", "8"),
        ("60181505", VIN, "FPO1180181", "OPEN", "100"),
    ])
    assert [row["PO_NUMBER"] for row in analysis.load_open_pos(path)] == [
        "FPO1180182", "FPO1180181",
    ]


@pytest.mark.parametrize("age", ["", "-1", "eight", "8.5"])
def test_invalid_age_stops(tmp_path, age):
    path = export(tmp_path, [("60181505", VIN, PO, "OPEN", age)])
    with pytest.raises(ValueError, match="PO_DAYS_OPEN"):
        analysis.load_open_pos(path)


def test_no_eligible_age_stops(tmp_path):
    path = export(tmp_path, [("60181505", VIN, PO, "OPEN", "7")])
    with pytest.raises(ValueError, match="no open POs older than 7 days"):
        analysis.load_open_pos(path)


def test_configurable_cutoff_and_age_omitted_from_results(tmp_path):
    path = export(tmp_path, [
        ("60181505", VIN, "FPO1180184", "OPEN", "14"),
        ("60181505", VIN, "FPO1180183", "OPEN", "15"),
    ])
    rows = analysis.load_open_pos(path, older_than_days=14)
    assert [row["PO_NUMBER"] for row in rows] == ["FPO1180183"]
    assert all("PO_DAYS_OPEN" not in row for row in rows)
    assert all("PO_DAYS_OPEN" not in row for row in analysis.summarize(rows, scan(folder(), rows)))
    with pytest.raises(ValueError, match="nonnegative"):
        analysis.load_open_pos(path, older_than_days=-1)


def test_negative_cli_cutoff_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc:
        analysis.main(["--input", str(tmp_path / "unused.xlsx"), "--older-than-days", "-1"])
    assert exc.value.code == 2


@pytest.mark.parametrize("rows, error", [
    ([("60181505", VIN, PO, "CLOSED")], "expected PO_STATUS=OPEN"),
    ([("60181505", VIN, PO, "OPEN")] * 2, "duplicate PO"),
    ([("60181505", "clipped", PO, "OPEN")], "invalid PO number or VIN"),
    ([], "no open POs"),
])
def test_invalid_export_stops(tmp_path, rows, error):
    with pytest.raises(ValueError, match=error):
        analysis.load_open_pos(export(tmp_path, rows))


def pdf_attachment():
    return SimpleNamespace(
        FileName="invoice.pdf", SaveAsFile=lambda path: Path(path).write_bytes(b"test"),
    )


@pytest.mark.parametrize("subject", [
    "[External] Invoice #5459388",
    f"[External] Invoice #5470706 (PO # {PO})",
    "INVOICE COPY",
])
def test_recursive_invoice_match_without_requiring_po_in_subject(monkeypatch, subject):
    monkeypatch.setattr(
        analysis, "parse_invoice_pdf",
        lambda path: InvoicePdfData(PO, VIN, Decimal("340.00")),
    )
    result = scan(folder(children=[
        folder([mail(subject, attachments=[pdf_attachment()])], name=r"Inbox\AGN\Processed"),
    ]))
    assert result.folders == 2
    assert result.selected_messages == result.pdfs == 1
    assert not result.failures
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "Invoice found"


@pytest.mark.parametrize("po, vin, amount", [
    ("FPO11801849", VIN, "340.00"),
    (PO, "5FNRL6H6XTB033605", "340.00"),
    (PO, VIN, "339.99"),
])
def test_all_three_fields_must_match(monkeypatch, po, vin, amount):
    monkeypatch.setattr(
        analysis, "parse_invoice_pdf",
        lambda path: InvoicePdfData(po, vin, Decimal(amount)),
    )
    result = scan(folder([mail("Invoice #1", attachments=[pdf_attachment()])]))
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "inconclusive"
    assert result.evidence[0]["MATCH_TYPE"] == "MISMATCH"


def test_shared_vin_does_not_match_other_po(monkeypatch):
    monkeypatch.setattr(
        analysis, "parse_invoice_pdf",
        lambda path: InvoicePdfData(PO, VIN, Decimal("340.00")),
    )
    rows = orders() + [{**orders()[0], "PO_NUMBER": "FPO1180183"}]
    result = scan(folder([mail("Invoice #1", attachments=[pdf_attachment()])]), rows)
    assert [row["ANALYSIS_STATUS"] for row in analysis.summarize(rows, result)] == [
        "Invoice found", "inconclusive",
    ]


def test_subject_pdf_po_conflict_is_inconclusive(monkeypatch):
    monkeypatch.setattr(
        analysis, "parse_invoice_pdf",
        lambda path: InvoicePdfData(PO, VIN, Decimal("340.00")),
    )
    result = scan(folder([
        mail("[External] Invoice #1 (PO # FPO1180183)", attachments=[pdf_attachment()]),
    ]))
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "inconclusive"


@pytest.mark.parametrize("subject,name,address", [
    ("Invoice #1", "Other vendor", "noreply@omegaedi.com"),
    ("Invoice #1", "AGN GLASS LLC", "other@example.com"),
    ("Quote", "AGN GLASS LLC", "noreply@omegaedi.com"),
])
def test_unselected_attachments_are_never_accessed(subject, name, address):
    class UnselectedMail:
        Class = 43
        Subject = subject
        SenderName = name
        SenderEmailAddress = address

        @property
        def Attachments(self):
            pytest.fail("Unselected email attachments must not be accessed")

    assert scan(folder([UnselectedMail()])).selected_messages == 0


def test_unreadable_invoice_reported_and_unmatched_inconclusive(monkeypatch):
    def parse(path):
        raise ValueError("No searchable invoice text")

    monkeypatch.setattr(analysis, "parse_invoice_pdf", parse)
    result = scan(folder([
        mail("Invoice #1", attachments=[pdf_attachment()]),
        mail("Invoice #2"),
    ]))
    assert len(result.failures) == 2
    assert result.selected_messages == 2
    assert "No searchable invoice text" in result.failures[0]["DETAIL"]
    assert "no PDF" in result.failures[1]["DETAIL"]
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "inconclusive"


def test_valid_match_survives_other_unreadable_invoice(monkeypatch):
    monkeypatch.setattr(
        analysis, "parse_invoice_pdf",
        lambda path: InvoicePdfData(PO, VIN, Decimal("340.00")),
    )
    result = scan(folder([
        mail("Invoice #1"), mail("Invoice #2", attachments=[pdf_attachment()]),
    ]))
    assert len(result.failures) == 1
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "Invoice found"


def test_non_agn_supplier_is_outside_scope():
    rows = [{**orders()[0], "SUPPLIER_NAME": "SUPERGLASS WINDSHIELD REPAIR INC"}]
    result = scan(folder(), rows)
    assert analysis.summarize(rows, result)[0]["ANALYSIS_STATUS"] == "inconclusive"


@pytest.mark.parametrize("amount", ["", "NaN", "Infinity", "-1", "1.001", "abc"])
def test_invalid_export_amount_rejected(tmp_path, amount):
    path = export(tmp_path, [("60181505", VIN, PO, "OPEN", "8", amount, "AGN GLASS LLC")])
    with pytest.raises(ValueError, match="PO_ESTIMATE_AMOUNT"):
        analysis.load_open_pos(path)


def test_subject_read_failure_stops():
    class BrokenMail:
        Class = 43
        EntryID = "broken"

        @property
        def Subject(self):
            raise PermissionError("subject unavailable")

    with pytest.raises(RuntimeError, match="Inbox.*subject unavailable"):
        scan(folder([BrokenMail()]))


def test_folder_failure_stops():
    class BrokenFolder:
        FolderPath = r"Inbox\Restricted"

        @property
        def Items(self):
            raise PermissionError("access denied")

    with pytest.raises(RuntimeError, match="Restricted.*access denied"):
        scan(folder(children=[BrokenFolder()]))


def test_no_match_is_scope_limited():
    result = scan(folder([mail("Unrelated")]))
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "Invoice not found"


def test_subject_without_invoice_is_outside_scope():
    result = scan(folder([mail(f"Purchase order {PO}")]))
    assert analysis.summarize(orders(), result)[0]["ANALYSIS_STATUS"] == "Invoice not found"


def test_account_must_match_exactly():
    with pytest.raises(ValueError, match="exactly one"):
        analysis.get_inbox(SimpleNamespace(Folders=[]), "missing")


def test_csv_neutralizes_email_formulas(tmp_path):
    path = tmp_path / "evidence.csv"
    analysis.write_csv(path, [{"SUBJECT": " =HYPERLINK(\"bad\")"}], ("SUBJECT",))
    assert "' =HYPERLINK" in path.read_text(encoding="utf-8-sig")


def test_cli_failure_returns_nonzero_without_completed_report(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "ROOT", tmp_path)
    path = export(tmp_path, [("60181505", VIN, PO, "OPEN")])
    output = tmp_path / "reports"
    assert analysis.main(["--input", str(path), "--output-dir", str(output)]) == 1
    assert list(output.glob("*/outstanding_pos.csv"))
    assert not list(output.glob("*/completed.json"))
    assert not list(output.glob("*/analysis.csv"))
