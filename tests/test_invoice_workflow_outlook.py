from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from invoice_workflow import invoice_parser
from invoice_workflow.invoice_parser import InvoicePdfData
from invoice_workflow.outlook_source import (
    INTERNET_MESSAGE_ID_PROPERTY,
    OutlookInvoiceSource,
)


def _folder_tree(messages):
    processed = SimpleNamespace(Name="Processed", Items=[])
    needs_review = SimpleNamespace(Name="Needs Review", Items=[])
    invoice = SimpleNamespace(
        Name="Invoice",
        Items=messages,
        Folders=[processed, needs_review],
    )
    invoice.Folders = _NamedFolders([processed, needs_review])
    agn = SimpleNamespace(Name="AGN", Folders=_NamedFolders([invoice]))
    inbox = SimpleNamespace(Name="Inbox", Folders=_NamedFolders([agn]))
    account = SimpleNamespace(Name="Dirk.Steele@avisbudget.com", Folders=_NamedFolders([inbox]))
    namespace = SimpleNamespace(Folders=[account])
    return namespace, processed, needs_review


def _find_invoice_folder(namespace):
    return namespace.Folders[0].Folders["Inbox"].Folders["AGN"].Folders["Invoice"]


class _NamedFolders(list):
    def __getitem__(self, name):
        if isinstance(name, str):
            return next(folder for folder in self if folder.Name == name)
        return super().__getitem__(name)

    def Add(self, name):
        folder = SimpleNamespace(Name=name, Items=[])
        self.append(folder)
        return folder


def _message(
    subject,
    message_id="<message-1@example>",
    attachments=1,
    received_at=datetime(2026, 9, 3, 14, 25, tzinfo=UTC),
):
    pdfs = []
    for index in range(attachments):
        attachment = MagicMock()
        attachment.FileName = f"invoice-{index}.pdf"
        pdfs.append(attachment)
    message = MagicMock()
    message.Class = 43
    message.Subject = subject
    message.EntryID = "entry-1"
    message.ReceivedTime = received_at
    message.Attachments = pdfs
    message.PropertyAccessor.GetProperty.return_value = message_id
    return message


def _mock_pdf_text(monkeypatch, text):
    pdf = MagicMock()
    pdf.__enter__.return_value.pages = [SimpleNamespace(extract_text=lambda: text)]
    monkeypatch.setattr(invoice_parser.pdfplumber, "open", lambda path: pdf)


def test_invoice_parser_allows_missing_vin_only_when_requested(monkeypatch):
    _mock_pdf_text(
        monkeypatch,
        "Invoice #5294412\nPO # FPO1131464\nSubtotal $340.00",
    )

    with pytest.raises(invoice_parser.InvoicePdfParseError, match="exactly one VIN"):
        invoice_parser.parse_invoice_pdf("invoice.pdf")

    invoice = invoice_parser.parse_invoice_pdf(
        "invoice.pdf",
        allow_missing_vin=True,
    )

    assert invoice.po_number == "FPO1131464"
    assert invoice.vin == ""
    assert invoice.amount == Decimal("340.00")


def test_invoice_parser_extracts_total_separately_and_requires_it_when_requested(
    monkeypatch,
):
    _mock_pdf_text(
        monkeypatch,
        "Invoice #5294412\nPO # FPO1131464\n"
        "VIN 1C4SJSBP6SS537975\nSubtotal $340.00\nTotal $365.00",
    )

    invoice = invoice_parser.parse_invoice_pdf("invoice.pdf", require_total=True)

    assert invoice.amount == Decimal("340.00")
    assert invoice.total_amount == Decimal("365.00")


def test_invoice_parser_requires_one_explicit_total_when_requested(monkeypatch):
    _mock_pdf_text(
        monkeypatch,
        "Invoice #5294412\nPO # FPO1131464\n"
        "VIN 1C4SJSBP6SS537975\nSubtotal $340.00",
    )

    with pytest.raises(invoice_parser.InvoicePdfParseError, match="invoice total"):
        invoice_parser.parse_invoice_pdf("invoice.pdf", require_total=True)


def test_invoice_parser_rejects_multiple_vins(monkeypatch):
    _mock_pdf_text(
        monkeypatch,
        "Invoice #5294412\nPO # FPO1131464\n"
        "VIN 1C4SJSBP6SS537975\nVIN 1FTFW1ET1EFA23456\nSubtotal $340.00",
    )

    with pytest.raises(invoice_parser.InvoicePdfParseError, match="at most one VIN"):
        invoice_parser.parse_invoice_pdf("invoice.pdf", allow_missing_vin=True)


def _recalibration_text(address="1501 Aviation Way", amount="150.00"):
    return (
        "Invoice #5426981\nPO # FPO1165259\n"
        "Customer Information Vehicle Information\n"
        "Customer Name Avis Rental Year 2025\n"
        f"Address {address} Model Telluride\n"
        "Augusta, GA 30906 VIN 5XYP6DGC9SG688906\n"
        "Telephone 706-798-1383 Unit # 60872862\n"
        "Quantity UOMList Extended Part Number Description\n"
        "1.00 ea $375.00 $150.00 RECAL-RTL-STATIC ADAS Recalibration (Static) $150.00\n"
        f"Subtotal ${amount}\n"
    )


def test_recalibration_parses_description_and_customer_address(monkeypatch):
    _mock_pdf_text(monkeypatch, _recalibration_text())
    invoice = invoice_parser.parse_invoice_pdf("invoice.pdf")
    assert invoice.is_recalibration
    assert invoice.customer_address == "1501 Aviation Way"
    invoice_parser.validate_vendor_invoice(invoice)


@pytest.mark.parametrize(
    ("address", "amount", "reason"),
    [
        ("3333 Bobby Brown Parkway", "150.00", "not permitted for BB"),
        ("4340 Global Gateway Connector", "150.00", "not permitted for APO"),
        ("1501 Aviation Way", "149.99", "exactly \\$150.00"),
        ("1501 Aviation Way", "150.01", "exactly \\$150.00"),
        ("", "150.00", "address is missing"),
    ],
)
def test_recalibration_policy_failures_are_visible_at_capture(
    monkeypatch, tmp_path, address, amount, reason
):
    _mock_pdf_text(monkeypatch, _recalibration_text(address, amount))
    message = _message("[External] Invoice #5426981 (PO # FPO1165259)")
    namespace, _, _ = _folder_tree([message])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
    )
    result = source.scan_parent_invoices()
    assert result.invoices == ()
    assert len(result.failures) == 1
    assert re.search(reason, result.failures[0].detail)
    message.Move.assert_not_called()


def test_amount_and_warranty_text_do_not_identify_recalibration(monkeypatch):
    text = _recalibration_text("3333 Bobby Brown Parkway").replace(
        "RECAL-RTL-STATIC ADAS Recalibration (Static)", "FW05978GTYN Windshield"
    )
    text += "Recalibrations completed by Auto Glass Now are warranted for Thirty days."
    _mock_pdf_text(monkeypatch, text)
    invoice = invoice_parser.parse_invoice_pdf("invoice.pdf")
    assert not invoice.is_recalibration
    invoice_parser.validate_vendor_invoice(invoice)


def test_recalibration_capture_uses_customer_not_billing_address(monkeypatch, tmp_path):
    _mock_pdf_text(
        monkeypatch,
        "Bill To: Avis Rental AP College Park GA\n4340 Global Gateway Connector\n"
        + _recalibration_text(),
    )
    message = _message("[External] Invoice #5426981 (PO # FPO1165259)")
    namespace, _, _ = _folder_tree([message])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
    )
    result = source.scan_parent_invoices()
    assert result.failures == ()
    assert len(result.invoices) == 1
    assert result.invoices[0].invoice_amount == Decimal("150.00")
    assert result.invoices[0].pdf_po_number == "FPO1165259"
    message.Move.assert_not_called()


@pytest.mark.parametrize("address", [
    "3333 Bobby Brown Parkway",
    "4340 Global Gateway Connector",
])
def test_billing_address_cannot_hide_in_house_customer(monkeypatch, address):
    _mock_pdf_text(
        monkeypatch,
        "Bill To: Avis Rental Augusta GA\n1501 Aviation Way\n"
        + _recalibration_text(address),
    )
    invoice = invoice_parser.parse_invoice_pdf("invoice.pdf")
    with pytest.raises(invoice_parser.InvoicePdfParseError, match="performed in-house"):
        invoice_parser.validate_vendor_invoice(invoice)


def test_scan_captures_only_exact_invoice_messages(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    receipt = _message("[External] Receipt for Job #5294412")
    namespace, _, _ = _folder_tree([receipt, invoice])
    parser = MagicMock(
        return_value=InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        )
    )
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=parser,
    )

    result = source.scan_parent_invoices()

    assert len(result.invoices) == 1
    assert result.failures == ()
    assert result.invoices[0].invoice_number == "5294412"
    assert result.invoices[0].subject_po_number == "FPO1131464"
    assert result.invoices[0].pdf_po_number == "FPO1131464"
    invoice.PropertyAccessor.GetProperty.assert_called_once_with(
        INTERNET_MESSAGE_ID_PROPERTY
    )
    receipt.PropertyAccessor.GetProperty.assert_not_called()


def test_scan_parent_includes_processed_child_folder(tmp_path):
    invoice = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<parent@example>",
    )
    processed_invoice = _message(
        "[External] Invoice #5294413 (PO # FPO1131465)",
        "<processed@example>",
    )
    namespace, processed, _ = _folder_tree([invoice])
    processed.Items.append(processed_invoice)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    default_result = source.scan_parent_invoices()
    assert [invoice.internet_message_id for invoice in default_result.invoices] == [
        "<parent@example>"
    ]

    result = source.scan_parent_invoices(include_processed=True)

    assert {invoice.internet_message_id for invoice in result.invoices} == {
        "<parent@example>",
        "<processed@example>",
    }
    assert result.failures == ()


def test_scan_parent_can_include_needs_review_child_folder(tmp_path):
    invoice = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<parent@example>",
    )
    review_invoice = _message(
        "[External] Invoice #5294413 (PO # FPO1131465)",
        "<review@example>",
    )
    namespace, _, needs_review = _folder_tree([invoice])
    needs_review.Items.append(review_invoice)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
            total_amount=Decimal("340.00"),
        ),
    )

    progress = []
    result = source.scan_parent_invoices(
        include_processed=True,
        include_needs_review=True,
        progress=progress.append,
    )

    assert {invoice.internet_message_id for invoice in result.invoices} == {
        "<parent@example>",
        "<review@example>",
    }
    assert any("Listing messages in Invoice" in message for message in progress)
    assert any("Found 1 invoice email candidate(s) in Invoice" in message for message in progress)
    assert any("Reading 2 invoice PDF(s)" in message for message in progress)
    assert not any("Reading PDF:" in message for message in progress)
    assert not any("Captured:" in message for message in progress)


def test_archive_moves_only_processed_messages_older_than_30_days(tmp_path):
    now = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    expired = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<expired@example>",
        received_at=now,
    )
    expired.SentOn = now - timedelta(days=30, seconds=1)
    exactly_30_days_old = _message(
        "[External] Invoice #5294413 (PO # FPO1131465)",
        "<boundary@example>",
        received_at=now,
    )
    exactly_30_days_old.SentOn = now - timedelta(days=30)
    active = _message(
        "[External] Invoice #5294414 (PO # FPO1131466)",
        "<active@example>",
        received_at=now,
    )
    active.SentOn = now - timedelta(days=60)
    review = _message(
        "[External] Invoice #5294415 (PO # FPO1131467)",
        "<review@example>",
        received_at=now,
    )
    review.SentOn = now - timedelta(days=60)
    namespace, processed, needs_review = _folder_tree([active])
    processed.Items.extend([expired, exactly_30_days_old])
    needs_review.Items.append(review)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
    )

    def move_to_archive(folder):
        folder.Parent = _find_invoice_folder(namespace)
        return SimpleNamespace(Parent=folder)

    expired.Move.side_effect = move_to_archive

    archived = source.archive_expired_processed_invoices(now=now)

    archive = next(
        folder
        for folder in _find_invoice_folder(namespace).Folders
        if folder.Name == "Invoice_Archive"
    )
    assert archived == 1
    expired.Move.assert_called_once_with(archive)
    exactly_30_days_old.Move.assert_not_called()
    active.Move.assert_not_called()
    review.Move.assert_not_called()


def test_archive_does_not_scan_archive_folder(tmp_path):
    namespace, processed, _ = _folder_tree([])
    invoice_folder = _find_invoice_folder(namespace)
    archive = SimpleNamespace(Name="Invoice_Archive", Items=[])
    archive.Parent = invoice_folder
    invoice_folder.Folders.append(archive)
    archived_message = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<archived@example>",
    )
    archive.Items.append(archived_message)
    parser = MagicMock()
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=parser,
    )

    result = source.scan_parent_invoices(include_processed=True)

    assert result.invoices == ()
    assert result.failures == ()
    parser.assert_not_called()


def test_archive_rejects_missing_sent_on_before_moving_any_message(tmp_path):
    expired = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    expired.SentOn = datetime(2026, 1, 1, tzinfo=UTC)
    invalid = _message(
        "[External] Invoice #5294413 (PO # FPO1131465)",
        "<invalid@example>",
    )
    invalid.SentOn = None
    namespace, processed, _ = _folder_tree([])
    processed.Items.extend([expired, invalid])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
    )

    with pytest.raises(ValueError, match="Outlook SentOn is missing or invalid"):
        source.archive_expired_processed_invoices(
            now=datetime(2026, 10, 31, tzinfo=UTC)
        )

    expired.Move.assert_not_called()
    invalid.Move.assert_not_called()


def test_archive_requires_confirmed_move_to_invoice_archive(tmp_path):
    expired = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    expired.SentOn = datetime(2026, 1, 1, tzinfo=UTC)
    namespace, processed, _ = _folder_tree([])
    processed.Items.append(expired)
    wrong_parent = SimpleNamespace(Name="Processed")
    expired.Move.return_value = SimpleNamespace(Parent=wrong_parent)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
    )

    with pytest.raises(RuntimeError, match="archive move was not confirmed"):
        source.archive_expired_processed_invoices(
            now=datetime(2026, 10, 31, tzinfo=UTC)
        )

    expired.Move.assert_called_once()


def test_move_to_processed_does_not_move_message_already_there(tmp_path):
    processed_invoice = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<processed@example>",
    )
    namespace, processed, _ = _folder_tree([])
    processed.Items.append(processed_invoice)
    processed_invoice.Parent = processed
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )
    source.scan_parent_invoices(include_processed=True)

    source.move_to_processed("<processed@example>")

    processed_invoice.Move.assert_not_called()


def test_scan_needs_review_reads_only_exact_new_format_without_moves(tmp_path):
    parent_invoice = _message(
        "[External] Invoice #5294411 (PO # FPO1131463)",
        "<parent@example>",
    )
    exact_review = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<review@example>",
    )
    subject_without_po = _message(
        "[External] Invoice #5294414",
        "<review-without-po@example>",
    )
    malformed_subject_po = _message(
        "[External] Invoice #5294415 (PO # FPO11314A4)",
        "<review-malformed-po@example>",
    )
    old_format_review = _message("Invoice #5294413", "<old@example>")
    namespace, _, needs_review = _folder_tree([parent_invoice])
    needs_review.Items = [
        old_format_review, exact_review, subject_without_po, malformed_subject_po,
    ]
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_needs_review_invoices()

    assert [invoice.internet_message_id for invoice in result.invoices] == [
        "<review@example>",
        "<review-without-po@example>",
    ]
    assert len(result.failures) == 1
    assert result.failures[0].internet_message_id == "<review-malformed-po@example>"
    assert "invalid or malformed PO number" in result.failures[0].detail
    parent_invoice.Attachments[0].SaveAsFile.assert_not_called()
    old_format_review.Attachments[0].SaveAsFile.assert_not_called()
    exact_review.Move.assert_not_called()
    subject_without_po.Move.assert_not_called()
    malformed_subject_po.Attachments[0].SaveAsFile.assert_not_called()


def test_scanned_needs_review_message_moves_to_processed_by_exact_id(tmp_path):
    exact_review = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        "<review@example>",
    )
    namespace, processed, needs_review = _folder_tree([])
    needs_review.Items = [exact_review]
    exact_review.Move.return_value = SimpleNamespace(Parent=processed)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )
    source.scan_needs_review_invoices()

    source.move_to_processed("<review@example>")

    exact_review.Move.assert_called_once_with(processed)


def test_scan_reports_multiple_pdf_attachments_without_parsing(tmp_path):
    invoice = _message(
        "[External] Invoice #5294412 (PO # FPO1131464)",
        attachments=2,
    )
    namespace, _, _ = _folder_tree([invoice])
    parser = MagicMock()
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=parser,
    )

    result = source.scan_parent_invoices()

    assert result.invoices == ()
    assert "Expected exactly one PDF attachment" in result.failures[0].detail
    parser.assert_not_called()


def test_scan_subject_po_ignores_trailing_punctuation(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO1131464-)")
    namespace, _, _ = _folder_tree([invoice])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_parent_invoices()

    assert len(result.invoices) == 1
    assert result.failures == ()
    assert result.invoices[0].subject_po_number == "FPO1131464"


def test_scan_parent_accepts_invoice_subject_without_po(tmp_path):
    invoice = _message("[External] Invoice #5294412")
    namespace, _, _ = _folder_tree([invoice])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_parent_invoices()

    assert len(result.invoices) == 1
    assert result.failures == ()
    assert result.invoices[0].subject_po_number == ""
    assert result.invoices[0].pdf_po_number == "FPO1131464"


def test_scan_parent_rejects_malformed_subject_po(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO11314A4)")
    namespace, _, _ = _folder_tree([invoice])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_parent_invoices()

    assert result.invoices == ()
    assert "invalid or malformed PO number" in result.failures[0].detail


def test_scan_excludes_terminal_ids_before_max_invoices(tmp_path):
    messages = [
        _message(
            f"[External] Invoice #{invoice_number} (PO # FPO1131464)",
            message_id,
            received_at=datetime(2026, 9, day, tzinfo=UTC),
        )
        for day, invoice_number, message_id in (
            (1, "5294411", "<terminal@example>"),
            (2, "5294412", "<retryable@example>"),
            (3, "5294413", "<unprocessed@example>"),
        )
    ]
    namespace, _, _ = _folder_tree(messages)
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_parent_invoices(
        max_invoices=2,
        excluded_message_ids=frozenset({"<terminal@example>"}),
    )

    assert [invoice.internet_message_id for invoice in result.invoices] == [
        "<retryable@example>",
        "<unprocessed@example>",
    ]
    messages[0].Attachments[0].SaveAsFile.assert_not_called()


@pytest.mark.parametrize("missing_pdf", [False, True])
def test_single_invoice_limit_applies_before_capture_and_all_closeout_mutations(
    monkeypatch, tmp_path, missing_pdf,
):
    import argparse
    import json
    from invoice_workflow import cli
    from WorkItems import build_close_queue, close_workitem
    from vendor_tracking import sheet_updater

    messages = [
        _message(
            f"[External] Invoice #{5485994 + index} (PO # FPO1178730)",
            f"<invoice-{index}@example>",
            attachments=0 if index == 0 and missing_pdf else 1,
            received_at=datetime(2026, 10, 22, 15, index, tzinfo=UTC),
        )
        for index in range(17)
    ]
    namespace, _, _ = _folder_tree(list(reversed(messages)))
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path / "attachments",
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1178730", vin="5XYP64GC1SG678709",
            amount=Decimal("340.00"),
        ),
    )
    scan = source.scan_parent_invoices(max_invoices=1)
    assert len(scan.invoices) + len(scan.failures) == 1
    for message in messages[1:]:
        message.Attachments[0].SaveAsFile.assert_not_called()

    queue = tmp_path / "close.csv"
    queue.write_text("mva,Type\n011111111,Glass\n", encoding="utf-8")
    monkeypatch.setattr(build_close_queue, "DEFAULT_CLOSE_QUEUE", queue)
    monkeypatch.setattr(build_close_queue, "DEFAULT_CLOSE_HISTORY", tmp_path / "history.csv")
    monkeypatch.setattr(build_close_queue, "PROCESSED_STATE_PATH", tmp_path / "state.json")
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.return_value = {"MVA": "59235573", "Inventory Date": "2026-10-22"}
    updater_factory = MagicMock(return_value=updater)
    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", updater_factory)
    results = tmp_path / "results.json"

    def run(command, **kwargs):
        targets = close_workitem._build_targets(argparse.Namespace(
            csv_path=str(queue), mvas="", max_rows=None,
            only_mvas=command[command.index("--only-mvas") + 1],
        ))
        assert targets == [{"mva": "059235573", "complaint_type": "Glass"}]
        results.write_text(json.dumps([{
            **target, "status": "closed", "detail": "",
        } for target in targets]), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    runner = MagicMock(side_effect=run)
    monkeypatch.setattr(cli.subprocess, "run", runner)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    if missing_pdf:
        assert cli._close_selected_work_items(scan, config, results, MagicMock()) == ([], set())
        updater_factory.assert_not_called()
        runner.assert_not_called()
        assert queue.read_text(encoding="utf-8") == "mva,Type\n011111111,Glass\n"
    else:
        cli._close_selected_work_items(scan, config, results, MagicMock())
        assert scan.invoices[0].invoice_number == "5485994"
        updater.find_rows_by_vin.assert_called_once_with("5XYP64GC1SG678709")
        updater.update_vendor_fields.assert_called_once()
        runner.assert_called_once()


def test_scan_reports_duplicate_message_id(tmp_path):
    first = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    second = _message("[External] Invoice #5294413 (PO # FPO1131465)")
    namespace, _, _ = _folder_tree([first, second])
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    result = source.scan_parent_invoices()

    assert len(result.invoices) == 1
    assert len(result.failures) == 1
    assert "Duplicate InternetMessageID" in result.failures[0].detail


def test_move_uses_captured_message_and_verifies_processed_parent(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    namespace, processed, _ = _folder_tree([invoice])
    moved = SimpleNamespace(Parent=processed)
    invoice.Move.return_value = moved
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )
    source.scan_parent_invoices()

    source.move_to_processed("<message-1@example>")

    invoice.Move.assert_called_once_with(processed)


def test_move_to_needs_review_uses_expected_folder(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    namespace, _, needs_review = _folder_tree([invoice])
    moved = SimpleNamespace(Parent=needs_review)
    invoice.Move.return_value = moved
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )
    source.scan_parent_invoices()

    source.move_to_needs_review("<message-1@example>")

    invoice.Move.assert_called_once_with(needs_review)


def test_move_to_needs_review_finds_message_without_prior_scan(tmp_path):
    invoice = _message("[External] Invoice #5294412 (PO # FPO1131464)")
    namespace, _, needs_review = _folder_tree([invoice])
    moved = SimpleNamespace(Parent=needs_review)
    invoice.Move.return_value = moved
    source = OutlookInvoiceSource(
        namespace,
        account_name="Dirk.Steele@avisbudget.com",
        attachment_directory=tmp_path,
        pdf_parser=lambda path: InvoicePdfData(
            po_number="FPO1131464",
            vin="1C4SJSBP6SS537975",
            amount=Decimal("340.00"),
        ),
    )

    source.move_to_needs_review("<message-1@example>")

    invoice.Move.assert_called_once_with(needs_review)
