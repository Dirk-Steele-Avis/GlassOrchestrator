from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from invoice_workflow.contracts import (
    CaptureFailure,
    FieldPOReviewSnapshot,
    OutlookScanResult,
    ParsedInvoice,
    ReviewHistory,
    StageStatus,
)
from invoice_workflow.review_report import (
    build_review_report,
    move_approved_to_processed,
    write_review_csv,
)


def _invoice(number: str, amount: str = "340.00") -> ParsedInvoice:
    return ParsedInvoice(
        internet_message_id=f"<{number}@example>",
        entry_id=f"entry-{number}",
        invoice_number=number,
        subject=f"[External] Invoice #{number} (PO # FPO1{number})",
        received_at=datetime(2026, 9, 1, tzinfo=UTC)
        + timedelta(seconds=int(number)),
        subject_po_number=f"FPO1{number}",
        pdf_po_number=f"FPO1{number}",
        vin="1C4SJSBP6SS537975",
        invoice_amount=Decimal(amount),
        pdf_path=f"{number}.pdf",
    )


def test_report_keeps_all_rows_and_prioritizes_approved_then_match():
    repository = MagicMock()
    repository.get_review_history.side_effect = lambda message_id: ReviewHistory(
        "identity",
        StageStatus.BLOCKED,
        "Authorized amount mismatch: invoice 340.00, FieldPO 315.00",
    )
    fieldpo = MagicMock()
    fieldpo.read_review.side_effect = [
        FieldPOReviewSnapshot("IN PROGRESS", Decimal("315.00")),
        FieldPOReviewSnapshot("APPROVED", Decimal("340.00")),
        FieldPOReviewSnapshot("IN PROGRESS", Decimal("340.00")),
    ]
    scan = OutlookScanResult(
        (_invoice("1000001"), _invoice("1000002"), _invoice("1000003")),
        (),
    )

    summary = build_review_report(scan, repository, fieldpo)

    assert [row.invoice_number for row in summary.rows] == [
        "1000002",
        "1000003",
        "1000001",
    ]
    assert summary.already_approved == 1
    assert summary.amount_matches == 2
    assert summary.amount_mismatches == 1
    assert all(row.historical_code == "AMOUNT_MISMATCH" for row in summary.rows)


def test_report_exposes_missing_history_and_capture_or_fieldpo_failures(tmp_path):
    repository = MagicMock()
    repository.get_review_history.return_value = None
    fieldpo = MagicMock()
    fieldpo.read_review.side_effect = RuntimeError("unknown PO status")
    scan = OutlookScanResult(
        (_invoice("1000001"),),
        (
            CaptureFailure(
                entry_id="entry-2",
                subject="[External] Invoice #1000002 (PO # FPO11000002)",
                detail="ValueError: Expected exactly one PDF attachment, found 2",
                internet_message_id="<1000002@example>",
            ),
        ),
    )

    summary = build_review_report(scan, repository, fieldpo)
    path = tmp_path / "invoice_review.csv"
    write_review_csv(summary.rows, path)

    assert len(summary.rows) == 2
    assert summary.history_not_found == 2
    assert summary.diagnostic_failures == 2
    assert summary.exit_code == 2
    assert {row.assessment_code for row in summary.rows} == {
        "FIELDPO_READ_FAILED",
        "CAPTURE_FAILED",
    }
    csv_text = path.read_text(encoding="utf-8")
    assert "historical_detail" in csv_text
    assert "unknown PO status" in csv_text


def test_move_approved_moves_only_approved_and_records_exact_failures():
    repository = MagicMock()
    repository.get_review_history.return_value = None
    fieldpo = MagicMock()
    fieldpo.read_review.side_effect = [
        FieldPOReviewSnapshot("APPROVED", Decimal("315.00")),
        FieldPOReviewSnapshot("IN PROGRESS", Decimal("340.00")),
        FieldPOReviewSnapshot("APPROVED", Decimal("340.00")),
    ]
    summary = build_review_report(
        OutlookScanResult(
            (_invoice("1000001"), _invoice("1000002"), _invoice("1000003")),
            (),
        ),
        repository,
        fieldpo,
    )
    outlook = MagicMock()
    outlook.move_to_processed.side_effect = [None, RuntimeError("move not confirmed")]

    moved = move_approved_to_processed(summary, outlook)

    assert [call.args[0] for call in outlook.move_to_processed.call_args_list] == [
        "<1000001@example>",
        "<1000003@example>",
    ]
    assert moved.processed_moved == 1
    assert moved.processed_move_failures == 1
    assert moved.exit_code == 2
    rows = {row.invoice_number: row for row in moved.rows}
    assert rows["1000001"].moved_to_processed is True
    assert rows["1000002"].moved_to_processed is False
    assert rows["1000002"].move_detail == ""
    assert "move not confirmed" in rows["1000003"].move_detail