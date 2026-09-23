"""Read-only assessment and CSV reporting for Needs Review invoices."""

from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from invoice_workflow.contracts import (
    FieldPOReviewReader,
    OutlookScanResult,
    OutlookSource,
    ParsedInvoice,
)
from invoice_workflow.failure_codes import classify_failure
from invoice_workflow.store import InvoiceRepository

_SUBJECT_PATTERN = re.compile(
    r"\[External\]\s+Invoice\s+#(?P<invoice>\d+)\s+"
    r"\(PO\s+#\s*(?P<po>FPO\d{7,})(?:[^\d)]*)\)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReviewReportRow:
    priority: int
    invoice_number: str
    received_utc: str
    subject: str
    internet_message_id: str
    subject_po_number: str
    pdf_po_number: str
    vin: str
    invoice_amount: str
    historical_stage: str
    historical_status: str
    historical_code: str
    historical_detail: str
    current_po_status: str
    current_authorized_amount: str
    already_approved: bool
    amount_match: bool
    assessment_code: str
    assessment_detail: str
    moved_to_processed: bool = False
    move_detail: str = ""


@dataclass(frozen=True, slots=True)
class ReviewReportSummary:
    rows: tuple[ReviewReportRow, ...]
    already_approved: int
    amount_matches: int
    amount_mismatches: int
    history_not_found: int
    diagnostic_failures: int
    processed_moved: int = 0
    processed_move_failures: int = 0

    @property
    def exit_code(self) -> int:
        return 2 if self.diagnostic_failures or self.processed_move_failures else 0


def build_review_report(
    scan: OutlookScanResult,
    repository: InvoiceRepository,
    fieldpo_reader: FieldPOReviewReader,
) -> ReviewReportSummary:
    rows = [
        _assess_invoice(invoice, repository, fieldpo_reader)
        for invoice in scan.invoices
    ]
    rows.extend(_capture_failure_row(failure) for failure in scan.failures)
    rows.sort(key=lambda row: (row.priority, row.received_utc, row.invoice_number))
    return _summarize(tuple(rows))


def move_approved_to_processed(
    summary: ReviewReportSummary,
    outlook_source: OutlookSource,
) -> ReviewReportSummary:
    """Move only exact messages whose current FieldPO state is APPROVED."""
    rows: list[ReviewReportRow] = []
    for row in summary.rows:
        if not row.already_approved:
            rows.append(row)
            continue
        try:
            outlook_source.move_to_processed(row.internet_message_id)
        except Exception as exc:
            rows.append(
                replace(row, move_detail=f"{type(exc).__name__}: {exc}")
            )
        else:
            rows.append(replace(row, moved_to_processed=True))
    return _summarize(tuple(rows))


def _summarize(rows: tuple[ReviewReportRow, ...]) -> ReviewReportSummary:
    return ReviewReportSummary(
        rows=rows,
        already_approved=sum(row.already_approved for row in rows),
        amount_matches=sum(row.amount_match for row in rows),
        amount_mismatches=sum(
            row.assessment_code == "AMOUNT_MISMATCH" for row in rows
        ),
        history_not_found=sum(
            row.historical_code == "HISTORY_NOT_FOUND" for row in rows
        ),
        diagnostic_failures=sum(
            row.assessment_code.endswith("_FAILED") for row in rows
        ),
        processed_moved=sum(row.moved_to_processed for row in rows),
        processed_move_failures=sum(bool(row.move_detail) for row in rows),
    )


def write_review_csv(rows: tuple[ReviewReportRow, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(ReviewReportRow.__slots__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    temporary_path.replace(path)


def _assess_invoice(
    invoice: ParsedInvoice,
    repository: InvoiceRepository,
    fieldpo_reader: FieldPOReviewReader,
) -> ReviewReportRow:
    history = repository.get_review_history(invoice.internet_message_id)
    historical_stage = history.stage if history else ""
    historical_status = str(history.status) if history else ""
    historical_detail = history.detail if history else ""
    historical_code = (
        classify_failure(historical_stage, historical_detail)
        if history
        else "HISTORY_NOT_FOUND"
    )
    try:
        current = fieldpo_reader.read_review(invoice.subject_po_number)
    except Exception as exc:
        return _row(
            invoice,
            priority=3,
            historical_stage=historical_stage,
            historical_status=historical_status,
            historical_code=historical_code,
            historical_detail=historical_detail,
            assessment_code="FIELDPO_READ_FAILED",
            assessment_detail=f"{type(exc).__name__}: {exc}",
        )

    approved = current.po_status == "APPROVED"
    amount_match = current.authorized_amount == invoice.invoice_amount
    assessment_code = (
        "ALREADY_APPROVED"
        if approved
        else "AMOUNT_MATCH"
        if amount_match
        else "AMOUNT_MISMATCH"
    )
    priority = 1 if approved else 2 if amount_match else 3
    return _row(
        invoice,
        priority=priority,
        historical_stage=historical_stage,
        historical_status=historical_status,
        historical_code=historical_code,
        historical_detail=historical_detail,
        current_po_status=current.po_status,
        current_authorized_amount=str(current.authorized_amount),
        already_approved=approved,
        amount_match=amount_match,
        assessment_code=assessment_code,
    )


def _row(
    invoice: ParsedInvoice,
    *,
    priority: int,
    historical_stage: str,
    historical_status: str,
    historical_code: str,
    historical_detail: str,
    current_po_status: str = "",
    current_authorized_amount: str = "",
    already_approved: bool = False,
    amount_match: bool = False,
    assessment_code: str,
    assessment_detail: str = "",
) -> ReviewReportRow:
    return ReviewReportRow(
        priority=priority,
        invoice_number=invoice.invoice_number,
        received_utc=_utc_text(invoice.received_at),
        subject=invoice.subject,
        internet_message_id=invoice.internet_message_id,
        subject_po_number=invoice.subject_po_number,
        pdf_po_number=invoice.pdf_po_number,
        vin=invoice.vin,
        invoice_amount=str(invoice.invoice_amount),
        historical_stage=historical_stage,
        historical_status=historical_status,
        historical_code=historical_code,
        historical_detail=historical_detail,
        current_po_status=current_po_status,
        current_authorized_amount=current_authorized_amount,
        already_approved=already_approved,
        amount_match=amount_match,
        assessment_code=assessment_code,
        assessment_detail=assessment_detail,
    )


def _capture_failure_row(failure) -> ReviewReportRow:
    match = _SUBJECT_PATTERN.fullmatch(failure.subject.strip())
    return ReviewReportRow(
        priority=3,
        invoice_number=match.group("invoice") if match else "",
        received_utc="",
        subject=failure.subject,
        internet_message_id=failure.internet_message_id,
        subject_po_number=match.group("po").upper() if match else "",
        pdf_po_number="",
        vin="",
        invoice_amount="",
        historical_stage="",
        historical_status="",
        historical_code="HISTORY_NOT_FOUND",
        historical_detail="",
        current_po_status="",
        current_authorized_amount="",
        already_approved=False,
        amount_match=False,
        assessment_code="CAPTURE_FAILED",
        assessment_detail=failure.detail,
    )


def _utc_text(value: datetime) -> str:
    return value.isoformat()