"""Exact Outlook capture path for completed AGN invoices."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from invoice_workflow.contracts import (
    CaptureFailure,
    OutlookScanResult,
    ParsedInvoice,
)
from invoice_workflow.invoice_parser import (
    InvoicePdfData,
    parse_invoice_pdf,
    validate_vendor_invoice,
)

INTERNET_MESSAGE_ID_PROPERTY = (
    "http://schemas.microsoft.com/mapi/proptag/0x1035001E"
)
_INVOICE_NUMBER_PATTERN = re.compile(r"\bInvoice\s+#(\d+)\b", re.IGNORECASE)
_EXACT_SUBJECT_PATTERN = re.compile(
    r"\[External\]\s+Invoice\s+#(?P<invoice>\d+)"
    r"(?:\s+\(PO\s+#\s*(?P<po>FPO\d{7,})(?:[^\d)]*)\))?",
    re.IGNORECASE,
)
_REVIEW_SUBJECT_PATTERN = re.compile(
    r"\[External\]\s+Invoice\s+#\d+(?:\s+\(PO\s+#\s*[^)]*\))?",
    re.IGNORECASE,
)
_SUBJECT_PO_MARKER_PATTERN = re.compile(r"\bPO\s*#", re.IGNORECASE)


class OutlookInvoiceSource:
    """Capture parent-folder invoices through one exact Outlook path."""

    ARCHIVE_FOLDER_NAME = "Invoice_Archive"

    def __init__(
        self,
        namespace: Any,
        *,
        account_name: str,
        attachment_directory: str | Path,
        pdf_parser: Callable[[str | Path], InvoicePdfData] = parse_invoice_pdf,
    ) -> None:
        self._namespace = namespace
        self._account_name = account_name
        self._attachment_directory = Path(attachment_directory)
        self._pdf_parser = pdf_parser
        self._messages_by_id: dict[str, Any] = {}

    def scan_parent_invoices(
        self,
        *,
        invoice_number: str | None = None,
        max_invoices: int | None = None,
        excluded_message_ids: frozenset[str] = frozenset(),
        include_processed: bool = False,
        include_needs_review: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> OutlookScanResult:
        if max_invoices is not None and max_invoices <= 0:
            raise ValueError("max_invoices must be greater than zero")
        if invoice_number is not None and not invoice_number.isdigit():
            raise ValueError("invoice_number must contain only digits")

        folders = [self._get_invoice_folder()]
        if include_processed:
            folders.append(self._get_existing_child_folder("Processed"))
        if include_needs_review:
            folders.append(self._get_child_folder("Needs Review"))
        return self._scan_folders(
            tuple(folders),
            invoice_number=invoice_number,
            max_invoices=max_invoices,
            excluded_message_ids=excluded_message_ids,
            exact_subject_only=False,
            progress=progress,
        )

    def scan_needs_review_invoices(self) -> OutlookScanResult:
        """Read exact new-format invoices currently in Needs Review."""
        folder = self._get_existing_child_folder("Needs Review")
        return self._scan_folders(
            (folder,),
            invoice_number=None,
            max_invoices=None,
            excluded_message_ids=frozenset(),
            exact_subject_only=True,
            progress=None,
        )

    def archive_expired_processed_invoices(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Move Processed emails sent more than 30 days ago into the archive."""
        archive_folder = self._get_or_create_archive_folder()
        processed_folder = self._get_existing_child_folder("Processed")
        reference_time = datetime.now(UTC) if now is None else now
        cutoff = _as_utc(reference_time, "archive reference time") - timedelta(
            days=30
        )

        expired_messages = []
        for message in list(processed_folder.Items):
            if getattr(message, "Class", None) != 43:
                continue
            sent_on = _as_utc(getattr(message, "SentOn", None), "SentOn")
            if sent_on < cutoff:
                expired_messages.append(message)

        for message in expired_messages:
            moved = message.Move(archive_folder)
            parent = getattr(moved, "Parent", None)
            parent_name = str(getattr(parent, "Name", "") or "")
            parent_parent_name = str(
                getattr(getattr(parent, "Parent", None), "Name", "") or ""
            )
            if (
                parent_name != self.ARCHIVE_FOLDER_NAME
                or parent_parent_name != "Invoice"
            ):
                raise RuntimeError(
                    "Outlook archive move was not confirmed for "
                    f"email {getattr(message, 'Subject', '')!r}"
                )
        return len(expired_messages)

    def _scan_folders(
        self,
        folders: tuple[Any, ...],
        *,
        invoice_number: str | None,
        max_invoices: int | None,
        excluded_message_ids: frozenset[str],
        exact_subject_only: bool,
        progress: Callable[[str], None] | None,
    ) -> OutlookScanResult:
        candidates: list[tuple[Any, str | None]] = []
        for folder in folders:
            _report_progress(progress, f"Listing messages in {folder.Name}...")
            folder_candidates = 0
            for message in list(folder.Items):
                if getattr(message, "Class", None) != 43:
                    continue
                subject = str(getattr(message, "Subject", "") or "")
                if exact_subject_only and _REVIEW_SUBJECT_PATTERN.fullmatch(subject.strip()) is None:
                    continue
                number_match = _INVOICE_NUMBER_PATTERN.search(subject)
                if number_match is None:
                    continue
                if invoice_number is not None and number_match.group(1) != invoice_number:
                    continue
                try:
                    message_id = _internet_message_id(message)
                except Exception:
                    message_id = None
                if message_id in excluded_message_ids:
                    continue
                candidates.append((message, message_id))
                folder_candidates += 1
            _report_progress(
                progress,
                f"Found {folder_candidates} invoice email candidate(s) in {folder.Name}.",
            )

        candidates.sort(key=lambda candidate: _received_at(candidate[0]))
        if max_invoices is not None:
            candidates = candidates[:max_invoices]

        self._attachment_directory.mkdir(parents=True, exist_ok=True)
        invoices: list[ParsedInvoice] = []
        failures: list[CaptureFailure] = []
        self._messages_by_id.clear()
        _report_progress(progress, f"Reading {len(candidates)} invoice PDF(s)...")
        for message, message_id in candidates:
            subject = str(getattr(message, "Subject", "") or "")
            entry_id = str(getattr(message, "EntryID", "") or "")
            try:
                invoices.append(
                    self._capture_message(message, subject, entry_id, message_id)
                )
            except Exception as exc:
                failures.append(
                    CaptureFailure(
                        entry_id=entry_id,
                        subject=subject,
                        detail=f"{type(exc).__name__}: {exc}",
                        internet_message_id=message_id or "",
                    )
                )
                if message_id:
                    self._messages_by_id.setdefault(message_id, message)

        return OutlookScanResult(tuple(invoices), tuple(failures))

    def move_to_processed(self, internet_message_id: str) -> None:
        self._move_to_folder(internet_message_id, "Processed")

    def move_to_needs_review(self, internet_message_id: str) -> None:
        self._move_to_folder(internet_message_id, "Needs Review")

    def _move_to_folder(self, internet_message_id: str, folder_name: str) -> None:
        message = self._messages_by_id.get(internet_message_id)
        if message is None:
            message = self._find_message_by_internet_message_id(internet_message_id)
            if message is not None:
                self._messages_by_id[internet_message_id] = message
        if message is None:
            raise LookupError(
                f"Exact InternetMessageID is not present in this run: {internet_message_id}"
            )
        target_folder = self._get_child_folder(folder_name)
        current_parent = getattr(message, "Parent", None)
        if str(getattr(current_parent, "Name", "") or "") == folder_name:
            return
        moved = message.Move(target_folder)
        moved_parent = str(getattr(getattr(moved, "Parent", None), "Name", "") or "")
        if moved_parent != folder_name:
            raise RuntimeError(
                f"Outlook move was not confirmed for folder {folder_name} and "
                f"InternetMessageID {internet_message_id}"
            )

    def _capture_message(
        self,
        message: Any,
        subject: str,
        entry_id: str,
        internet_message_id: str | None,
    ) -> ParsedInvoice:
        if internet_message_id is None:
            internet_message_id = _internet_message_id(message)
        if not internet_message_id:
            raise ValueError("InternetMessageID is missing")
        if internet_message_id in self._messages_by_id:
            raise ValueError(f"Duplicate InternetMessageID: {internet_message_id}")

        invoice_match = _INVOICE_NUMBER_PATTERN.search(subject)
        if invoice_match is None:
            raise ValueError("Subject does not contain an invoice number")
        exact_subject_match = _EXACT_SUBJECT_PATTERN.fullmatch(subject.strip())
        subject_po = (
            exact_subject_match.group("po").upper()
            if exact_subject_match and exact_subject_match.group("po")
            else ""
        )
        if _SUBJECT_PO_MARKER_PATTERN.search(subject) and not subject_po:
            raise ValueError("Subject contains an invalid or malformed PO number")

        pdf_attachments = [
            attachment
            for attachment in message.Attachments
            if str(getattr(attachment, "FileName", "") or "").lower().endswith(".pdf")
        ]
        if len(pdf_attachments) != 1:
            raise ValueError(
                f"Expected exactly one PDF attachment, found {len(pdf_attachments)}"
            )

        digest = hashlib.sha256(internet_message_id.encode("utf-8")).hexdigest()[:16]
        pdf_path = self._attachment_directory / (
            f"invoice_{invoice_match.group(1)}_{digest}.pdf"
        )
        pdf_attachments[0].SaveAsFile(str(pdf_path))
        pdf_data = self._pdf_parser(pdf_path)
        validate_vendor_invoice(pdf_data)

        parsed = ParsedInvoice(
            internet_message_id=internet_message_id,
            entry_id=entry_id,
            invoice_number=invoice_match.group(1),
            subject=subject,
            received_at=_received_at(message),
            subject_po_number=subject_po,
            pdf_po_number=pdf_data.po_number,
            vin=pdf_data.vin,
            invoice_amount=pdf_data.amount,
            pdf_path=str(pdf_path),
            is_recalibration=pdf_data.is_recalibration,
            invoice_total=pdf_data.total_amount,
        )
        self._messages_by_id[internet_message_id] = message
        return parsed

    def _get_invoice_folder(self) -> Any:
        account_matches = [
            folder
            for folder in self._namespace.Folders
            if str(folder.Name).casefold() == self._account_name.casefold()
        ]
        if len(account_matches) != 1:
            raise LookupError(
                f"Expected exactly one Outlook account '{self._account_name}', "
                f"found {len(account_matches)}"
            )
        return account_matches[0].Folders["Inbox"].Folders["AGN"].Folders["Invoice"]

    def _get_child_folder(self, folder_name: str) -> Any:
        invoice_folder = self._get_invoice_folder()
        matches = [
            folder
            for folder in invoice_folder.Folders
            if str(folder.Name) == folder_name
        ]
        if not matches and folder_name == "Needs Review":
            invoice_folder.Folders.Add(folder_name)
            matches = [
                folder
                for folder in invoice_folder.Folders
                if str(folder.Name) == folder_name
            ]
        if len(matches) != 1:
            raise LookupError(
                f"Expected exactly one Outlook {folder_name} folder, found {len(matches)}"
            )
        return matches[0]

    def _get_or_create_archive_folder(self) -> Any:
        invoice_folder = self._get_invoice_folder()
        matches = [
            folder
            for folder in invoice_folder.Folders
            if str(folder.Name) == self.ARCHIVE_FOLDER_NAME
        ]
        if not matches:
            invoice_folder.Folders.Add(self.ARCHIVE_FOLDER_NAME)
            matches = [
                folder
                for folder in invoice_folder.Folders
                if str(folder.Name) == self.ARCHIVE_FOLDER_NAME
            ]
        if len(matches) != 1:
            raise LookupError(
                "Expected exactly one Outlook Invoice_Archive folder under Invoice, "
                f"found {len(matches)}"
            )
        return matches[0]

    def _get_existing_child_folder(self, folder_name: str) -> Any:
        invoice_folder = self._get_invoice_folder()
        matches = [
            folder
            for folder in invoice_folder.Folders
            if str(folder.Name) == folder_name
        ]
        if len(matches) != 1:
            raise LookupError(
                f"Expected exactly one existing Outlook {folder_name} folder, "
                f"found {len(matches)}"
            )
        return matches[0]

    def _find_message_by_internet_message_id(self, internet_message_id: str) -> Any | None:
        if not internet_message_id:
            return None
        folder = self._get_invoice_folder()
        for message in list(folder.Items):
            if getattr(message, "Class", None) != 43:
                continue
            try:
                current_message_id = _internet_message_id(message)
            except Exception:
                continue
            if current_message_id == internet_message_id:
                return message
        return None


def _report_progress(
    progress: Callable[[str], None] | None,
    message: str,
) -> None:
    if progress is not None:
        progress(message)


def _received_at(message: Any) -> datetime:
    return _as_utc(getattr(message, "ReceivedTime", None), "ReceivedTime")


def _as_utc(value: Any, property_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"Outlook {property_name} is missing or invalid")
    return value.astimezone(UTC)


def _internet_message_id(message: Any) -> str:
    return str(
        message.PropertyAccessor.GetProperty(INTERNET_MESSAGE_ID_PROPERTY) or ""
    ).strip()
