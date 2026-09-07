from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from invoice_workflow.invoice_parser import InvoicePdfData
from invoice_workflow.outlook_source import (
    INTERNET_MESSAGE_ID_PROPERTY,
    OutlookInvoiceSource,
)


def _folder_tree(messages):
    processed = SimpleNamespace(Name="Processed")
    needs_review = SimpleNamespace(Name="Needs Review")
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


class _NamedFolders(list):
    def __getitem__(self, name):
        if isinstance(name, str):
            return next(folder for folder in self if folder.Name == name)
        return super().__getitem__(name)


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
