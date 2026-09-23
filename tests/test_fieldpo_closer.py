from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "outlook"
    / "FieldPO_Closer"
    / "FieldPO_Closer.py"
)
SPEC = spec_from_file_location("fieldpo_closer", MODULE_PATH)
fieldpo_closer = module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = fieldpo_closer
SPEC.loader.exec_module(fieldpo_closer)

COMBINED_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "outlook"
    / "FieldPO_Closer"
    / "CombinedPDFCloser.py"
)
COMBINED_SPEC = spec_from_file_location("combined_pdf_closer", COMBINED_MODULE_PATH)
combined_pdf_closer = module_from_spec(COMBINED_SPEC)
assert COMBINED_SPEC.loader is not None
sys.modules[COMBINED_SPEC.name] = combined_pdf_closer
COMBINED_SPEC.loader.exec_module(combined_pdf_closer)


def _invoice(vin="1C4SJSBP6SS537975", po="FPO1131464"):
    return SimpleNamespace(
        vin=vin,
        subject_po_number=po,
        pdf_po_number=po,
        invoice_amount=Decimal("340.00"),
        internet_message_id="<invoice@example>",
    )


def test_load_rows_treats_omitted_result_cell_as_blank(tmp_path):
    csv_path = tmp_path / "vins.csv"
    csv_path.write_text("VIN,Result\n1C4SJSBP6SS537975\n", encoding="utf-8")

    rows = fieldpo_closer.load_rows(csv_path)

    assert rows == [fieldpo_closer.VinRow("1C4SJSBP6SS537975", "")]


def test_process_rows_sets_missing_review_and_success():
    rows = [
        fieldpo_closer.VinRow("1C4SJSBP6SS537975", ""),
        fieldpo_closer.VinRow("1FTFW1ET1EFA23456", "Missing Invoice"),
        fieldpo_closer.VinRow("INVALID", ""),
        fieldpo_closer.VinRow("1HGCM82633A123456", "Success"),
    ]
    saves = []

    fieldpo_closer.process_rows(
        rows,
        [_invoice()],
        lambda invoice: "Success",
        lambda current: saves.append([row.result for row in current]),
    )

    assert [row.result for row in rows] == [
        "Success",
        "Missing Invoice",
        "Review",
        "Success",
    ]
    assert len(saves) == 3


def test_process_rows_groups_duplicate_vins_and_multiple_invoices(capsys):
    duplicate_vin = "1FTFW1ET1EFA23456"
    rows = [
        fieldpo_closer.VinRow("1C4SJSBP6SS537975", ""),
        fieldpo_closer.VinRow(duplicate_vin, ""),
        fieldpo_closer.VinRow(duplicate_vin, "Missing Invoice"),
    ]
    processor = MagicMock(return_value="Success")

    fieldpo_closer.process_rows(
        rows,
        [
            _invoice(vin="1C4SJSBP6SS537975", po="FPO1131464"),
            _invoice(vin="1C4SJSBP6SS537975", po="FPO1131465"),
            _invoice(vin=duplicate_vin, po="FPO1131466"),
        ],
        processor,
        lambda current: None,
    )

    assert [row.result for row in rows] == ["Success", "Success", "Success"]
    assert processor.call_count == 2
    assert len(processor.call_args_list[0].args[0]) == 2
    assert len(processor.call_args_list[1].args[0]) == 1
    output = capsys.readouterr().out
    assert "rows=2" in output


def test_process_rows_allows_duplicate_vin_with_completed_row():
    duplicate_vin = "1FTFW1ET1EFA23456"
    rows = [
        fieldpo_closer.VinRow(duplicate_vin, ""),
        fieldpo_closer.VinRow(duplicate_vin, "Success"),
    ]
    processor = MagicMock()

    fieldpo_closer.process_rows(
        rows,
        [_invoice(vin=duplicate_vin)],
        processor,
        lambda current: None,
    )

    assert [row.result for row in rows] == [processor.return_value, "Success"]
    processor.assert_called_once()


def test_fieldpo_invoice_group_closes_only_final_po(monkeypatch):
    invoices = [_invoice(po="FPO1131464"), _invoice(po="FPO1131465")]
    calls = []

    def process_one(page, invoice, source, **kwargs):
        calls.append((invoice.subject_po_number, kwargs))
        return "Success"

    monkeypatch.setattr(fieldpo_closer, "process_fieldpo_invoice", process_one)

    result = fieldpo_closer.process_fieldpo_invoices(MagicMock(), invoices, MagicMock())

    assert result == "Success"
    assert calls == [
        (
            "FPO1131464",
            {
                "expected_po_numbers": {"FPO1131464", "FPO1131465"},
                "close_work_order": False,
            },
        ),
        (
            "FPO1131465",
            {
                "expected_po_numbers": {"FPO1131464", "FPO1131465"},
                "close_work_order": True,
            },
        ),
    ]


def test_fieldpo_invoice_closes_and_moves_only_after_strict_gates(monkeypatch):
    invoice = _invoice()
    page = MagicMock()
    page.get_by_text.return_value.all_inner_texts.return_value = ["FPO1131464"]
    source = MagicMock()
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "057935872",
    )
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "po_exists", lambda page, po: True)
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "read_work_order_created_by",
        lambda page: "Steele, Dirk",
    )
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "click_into_po", lambda page, po: None)
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "read_po_status_and_amount",
        lambda page: ("IN PROGRESS", "340.00"),
    )
    approve = MagicMock(return_value=True)
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "approve_displayed_po", approve)

    result = fieldpo_closer.process_fieldpo_invoice(page, invoice, source)

    assert result == "Success"
    approve.assert_called_once()
    source.move_to_processed.assert_called_once_with("<invoice@example>")


def test_fieldpo_invoice_flags_other_listed_po_before_opening_target(monkeypatch):
    page = MagicMock()
    page.get_by_text.return_value.all_inner_texts.return_value = [
        "FPO1131464",
        "FPO1131465",
    ]
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "057935872",
    )
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "po_exists", lambda page, po: True)
    click_po = MagicMock()
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "click_into_po", click_po)

    result = fieldpo_closer.process_fieldpo_invoice(page, _invoice(), MagicMock())

    assert result == "Review"
    click_po.assert_not_called()


def test_parse_combined_pdf_skips_terms_page(monkeypatch, tmp_path):
    invoice_page = MagicMock()
    invoice_page.extract_text.return_value = """
Invoice #5225602
VIN 1G1ZD5STXRF177786
PO # FPO1112094
Subtotal $340.00
"""
    terms_page = MagicMock()
    terms_page.extract_text.return_value = "Warranty terms and conditions"
    pdf = MagicMock()
    pdf.pages = [invoice_page, terms_page]
    context = MagicMock()
    context.__enter__.return_value = pdf
    monkeypatch.setattr(combined_pdf_closer.pdfplumber, "open", lambda path: context)

    invoices = combined_pdf_closer.parse_combined_pdf(tmp_path / "combined.pdf")

    assert len(invoices) == 1
    assert invoices[0].invoice_number == "5225602"
    assert invoices[0].subject_po_number == "FPO1112094"
    assert invoices[0].vin == "1G1ZD5STXRF177786"
    assert invoices[0].invoice_amount == Decimal("340.00")


def test_parse_combined_pdf_rejects_partial_invoice_page(monkeypatch, tmp_path):
    page = MagicMock()
    page.extract_text.return_value = "Invoice #5225602\nPO # FPO1112094"
    pdf = MagicMock()
    pdf.pages = [page]
    context = MagicMock()
    context.__enter__.return_value = pdf
    monkeypatch.setattr(combined_pdf_closer.pdfplumber, "open", lambda path: context)

    try:
        combined_pdf_closer.parse_combined_pdf(tmp_path / "combined.pdf")
    except ValueError as exc:
        assert "Page 1 must contain exactly one" in str(exc)
    else:
        raise AssertionError("Expected partial invoice page to be rejected")


def test_group_by_vin_preserves_pdf_order():
    first = combined_pdf_closer.CombinedPdfInvoice(
        1, "1", "1G1ZD5STXRF177786", "FPO1112094", "FPO1112094",
        Decimal("340.00"), "local:1",
    )
    other = combined_pdf_closer.CombinedPdfInvoice(
        3, "2", "1N4BL4DV4SN428209", "FPO1112101", "FPO1112101",
        Decimal("340.00"), "local:2",
    )
    second = combined_pdf_closer.CombinedPdfInvoice(
        5, "3", "1G1ZD5STXRF177786", "FPO1112203", "FPO1112203",
        Decimal("150.00"), "local:3",
    )

    groups = combined_pdf_closer.group_by_vin([first, other, second])

    assert groups == [[first, second], [other]]