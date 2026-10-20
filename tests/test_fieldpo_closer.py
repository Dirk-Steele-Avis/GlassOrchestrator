from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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


@pytest.fixture(autouse=True)
def isolate_po_approval_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "PO_APPROVAL_AUDIT_FILE",
        str(tmp_path / "po_approval_audit.jsonl"),
    )

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
    csv_path = tmp_path / "pos.csv"
    csv_path.write_text("PO,VIN,Result\nFPO1131464,,\n", encoding="utf-8")

    rows = fieldpo_closer.load_rows("po", csv_path)

    assert rows == [fieldpo_closer.PoRow("FPO1131464", "")]


def test_save_rows_writes_resolved_vin(tmp_path):
    csv_path = tmp_path / "pos.csv"
    rows = [fieldpo_closer.PoRow("FPO1131464", "Success", "1C4SJSBP6SS537975")]

    fieldpo_closer.save_rows("po", rows, csv_path)

    assert csv_path.read_text(encoding="utf-8-sig").splitlines() == [
        "PO,VIN,Result",
        "FPO1131464,1C4SJSBP6SS537975,Success",
    ]


def test_main_requires_explicit_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["FieldPO_Closer.py"])

    with pytest.raises(SystemExit) as error:
        fieldpo_closer.main()

    assert error.value.code == 2


def test_main_resolves_review_vins_without_scanning_outlook(monkeypatch):
    rows = [fieldpo_closer.PoRow("FPO1131464", "Review")]
    playwright_manager = MagicMock()
    context = MagicMock()
    page = MagicMock()
    identity_reader = MagicMock()
    identity_reader.read_identity.return_value = SimpleNamespace(
        vin="1C4SJSBP6SS537975"
    )
    reader_factory = MagicMock(return_value=identity_reader)
    monkeypatch.setattr(sys, "argv", ["FieldPO_Closer.py", "--mode", "po"])
    monkeypatch.setattr(fieldpo_closer, "load_rows", lambda mode: rows)
    monkeypatch.setattr(fieldpo_closer, "_load_config", lambda: {"account_name": "test"})
    monkeypatch.setattr(fieldpo_closer.pythoncom, "CoInitialize", lambda: None)
    monkeypatch.setattr(fieldpo_closer.pythoncom, "CoUninitialize", lambda: None)
    monkeypatch.setattr(
        fieldpo_closer,
        "sync_playwright",
        lambda: playwright_manager,
    )
    playwright_manager.__enter__.return_value = MagicMock()
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(fieldpo_closer, "FieldPOBrowserIdentityReader", reader_factory)
    outlook_source = MagicMock()
    monkeypatch.setattr(fieldpo_closer, "OutlookInvoiceSource", outlook_source)

    result = fieldpo_closer.main()

    assert result == 0
    assert rows[0].result == "Review"
    assert rows[0].vin == "1C4SJSBP6SS537975"
    identity_reader.read_identity.assert_called_once_with("FPO1131464")
    outlook_source.assert_not_called()
    context.close.assert_called_once()


def test_process_rows_preserves_explicit_vin_mode():
    vin = "1C4SJSBP6SS537975"
    rows = [fieldpo_closer.VinRow(vin, "")]
    saved = []

    fieldpo_closer.process_rows(
        "vin",
        rows,
        [_invoice(vin=vin)],
        lambda invoices: "Success",
        lambda current: saved.append(current[0].result),
    )

    assert rows[0].result == "Success"
    assert saved == ["Success"]


def test_process_rows_sets_missing_review_and_success():
    rows = [
        fieldpo_closer.PoRow("FPO1131464", ""),
        fieldpo_closer.PoRow("FPO1131465", "Missing Invoice"),
        fieldpo_closer.PoRow("INVALID", ""),
        fieldpo_closer.PoRow("FPO1131466", "Success"),
    ]
    saves = []

    fieldpo_closer.process_rows(
        "po",
        rows,
        [_invoice()],
        lambda invoice: "Success",
        lambda current: saves.append([row.result for row in current]),
        lambda po_number: "1C4SJSBP6SS537975",
    )

    assert [row.result for row in rows] == [
        "Success",
        "Missing Invoice",
        "Review",
        "Success",
    ]
    assert len(saves) == 4


def test_process_rows_groups_duplicate_pos_and_multiple_invoices(capsys):
    duplicate_po = "FPO1131464"
    rows = [
        fieldpo_closer.PoRow(duplicate_po, ""),
        fieldpo_closer.PoRow(duplicate_po, "Missing Invoice"),
        fieldpo_closer.PoRow("FPO1131465", ""),
        fieldpo_closer.PoRow("FPO1131466", ""),
    ]
    processor = MagicMock(return_value="Success")

    fieldpo_closer.process_rows(
        "po",
        rows,
        [
            _invoice(vin="1C4SJSBP6SS537975", po=duplicate_po),
            _invoice(vin="1C4SJSBP6SS537975", po="FPO1131465"),
            _invoice(vin="1FTFW1ET1EFA23456", po="FPO1131466"),
        ],
        processor,
        lambda current: None,
        lambda po_number: (
            "1FTFW1ET1EFA23456" if po_number == "FPO1131466" else "1C4SJSBP6SS537975"
        ),
    )

    assert [row.result for row in rows] == [
        "Success",
        "Success",
        "Success",
        "Success",
    ]
    assert processor.call_count == 2
    grouped = {
        tuple(invoice.subject_po_number for invoice in call.args[0])
        for call in processor.call_args_list
    }
    assert grouped == {
        (duplicate_po, "FPO1131465"),
        ("FPO1131466",),
    }
    output = capsys.readouterr().out
    assert "rows=2" in output


def test_process_rows_allows_duplicate_po_with_completed_row():
    duplicate_po = "FPO1131464"
    rows = [
        fieldpo_closer.PoRow(duplicate_po, ""),
        fieldpo_closer.PoRow(duplicate_po, "Success"),
    ]
    processor = MagicMock()

    fieldpo_closer.process_rows(
        "po",
        rows,
        [_invoice(po=duplicate_po)],
        processor,
        lambda current: None,
        lambda po_number: "1C4SJSBP6SS537975",
    )

    assert [row.result for row in rows] == [processor.return_value, "Success"]
    processor.assert_called_once()


@pytest.mark.parametrize("invoice_count", [0, 2])
def test_process_rows_reviews_missing_or_ambiguous_invoice(invoice_count):
    po_number = "FPO1131464"
    rows = [fieldpo_closer.PoRow(po_number, "")]
    processor = MagicMock(return_value="Success")

    fieldpo_closer.process_rows(
        "po",
        rows,
        [_invoice(po=po_number) for _ in range(invoice_count)],
        processor,
        lambda current: None,
        lambda current: "1C4SJSBP6SS537975",
    )

    assert rows[0].result == ("Missing Invoice" if invoice_count == 0 else "Review")
    assert rows[0].vin == "1C4SJSBP6SS537975"
    processor.assert_not_called()


def test_process_rows_resolves_vin_for_review_row_without_invoice():
    po_number = "FPO1131464"
    rows = [fieldpo_closer.PoRow(po_number, "Review")]
    processor = MagicMock()

    fieldpo_closer.process_rows(
        "po",
        rows,
        [],
        processor,
        lambda current: None,
        lambda requested_po: "1C4SJSBP6SS537975",
    )

    assert rows[0].result == "Review"
    assert rows[0].vin == "1C4SJSBP6SS537975"
    processor.assert_not_called()


def test_process_rows_uses_fieldpo_vin_when_invoice_has_no_vin(capsys):
    fieldpo_vin = "1C4SJSBP6SS537975"
    invoice = _invoice(vin="")
    rows = [fieldpo_closer.PoRow("FPO1131464", "")]
    processor = MagicMock(return_value="Success")
    resolver = MagicMock(return_value=fieldpo_vin)

    fieldpo_closer.process_rows(
        "po",
        rows,
        [invoice],
        processor,
        lambda current: None,
        resolver,
    )

    assert rows[0].result == "Success"
    assert rows[0].vin == fieldpo_vin
    resolver.assert_called_once_with("FPO1131464")
    processor.assert_called_once_with([invoice])
    assert f"FPO1131464 VIN {fieldpo_vin}: Success" in capsys.readouterr().out


def test_process_rows_blocks_invoice_vin_mismatch_with_fieldpo():
    invoice = _invoice(vin="1FTFW1ET1EFA23456")
    rows = [fieldpo_closer.PoRow("FPO1131464", "")]
    processor = MagicMock(return_value="Success")

    fieldpo_closer.process_rows(
        "po",
        rows,
        [invoice],
        processor,
        lambda current: None,
        lambda current: "1C4SJSBP6SS537975",
    )

    assert rows[0].result == "Review"
    processor.assert_not_called()


def test_process_rows_resolves_vin_but_blocks_subject_pdf_po_mismatch():
    invoice = _invoice(po="FPO1131464")
    invoice.pdf_po_number = "FPO1131465"
    rows = [fieldpo_closer.PoRow("FPO1131464", "")]
    processor = MagicMock(return_value="Success")
    resolver = MagicMock(return_value="1C4SJSBP6SS537975")

    fieldpo_closer.process_rows(
        "po",
        rows,
        [invoice],
        processor,
        lambda current: None,
        resolver,
    )

    assert rows[0].result == "Review"
    assert rows[0].vin == "1C4SJSBP6SS537975"
    resolver.assert_called_once_with("FPO1131464")
    processor.assert_not_called()


def test_load_rows_requires_explicit_vin_or_po_mode(tmp_path):
    csv_path = tmp_path / "vins.csv"
    csv_path.write_text("VIN,Result\n1C4SJSBP6SS537975\n", encoding="utf-8")

    rows = fieldpo_closer.load_rows("vin", csv_path)

    assert rows == [fieldpo_closer.VinRow("1C4SJSBP6SS537975", "")]


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
                "fieldpo_vin": None,
            },
        ),
        (
            "FPO1131465",
            {
                "expected_po_numbers": {"FPO1131464", "FPO1131465"},
                "close_work_order": True,
                "fieldpo_vin": None,
            },
        ),
    ]


def test_fieldpo_invoice_uses_resolved_fieldpo_vin(monkeypatch):
    invoice = _invoice(vin="")
    page = MagicMock()
    page.get_by_text.return_value.all_inner_texts.return_value = ["FPO1131464"]
    navigated_vins = []
    approved_vins = []
    monkeypatch.setattr(
        fieldpo_closer.agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: navigated_vins.append(vin) or "057935872",
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

    def approve(page, vin, **kwargs):
        approved_vins.append(vin)
        return True

    monkeypatch.setattr(fieldpo_closer.agn_invoices, "approve_displayed_po", approve)

    result = fieldpo_closer.process_fieldpo_invoice(
        page,
        invoice,
        MagicMock(),
        fieldpo_vin="1C4SJSBP6SS537975",
    )

    assert result == "Success"
    assert navigated_vins == ["1C4SJSBP6SS537975"]
    assert approved_vins == ["1C4SJSBP6SS537975"]


def test_fieldpo_invoice_closes_and_moves_only_after_strict_gates(monkeypatch, tmp_path):
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
    audit = json.loads((tmp_path / "po_approval_audit.jsonl").read_text(encoding="utf-8"))
    assert audit["source"] == "outlook.FieldPO_Closer"
    assert audit["outcome"] == "approved"
    assert audit["approval_action"] is True
    assert audit["amount_match"] is True
    assert audit["work_order_closed"] is True


def test_fieldpo_invoice_blocks_live_amount_mismatch(monkeypatch, tmp_path):
    invoice = _invoice()
    page = MagicMock()
    page.get_by_text.return_value.all_inner_texts.return_value = ["FPO1131464"]
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
        lambda page: ("IN PROGRESS", "400.00"),
    )
    approve = MagicMock()
    monkeypatch.setattr(fieldpo_closer.agn_invoices, "approve_displayed_po", approve)

    result = fieldpo_closer.process_fieldpo_invoice(page, invoice, MagicMock())

    assert result == "Review"
    approve.assert_not_called()
    audit = json.loads((tmp_path / "po_approval_audit.jsonl").read_text(encoding="utf-8"))
    assert audit["outcome"] == "blocked"
    assert audit["reason"] == "amount_mismatch"
    assert audit["invoice_amount"] == "340.00"
    assert audit["authorized_amount"] == "400.00"
    assert audit["approval_action"] is False


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