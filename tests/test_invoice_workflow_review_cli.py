from pathlib import Path
from unittest.mock import MagicMock

from invoice_workflow import review_cli
from invoice_workflow.review_report import ReviewReportRow, ReviewReportSummary


def _summary() -> ReviewReportSummary:
    row = ReviewReportRow(
        priority=1,
        invoice_number="5294412",
        received_utc="2026-09-03T14:25:00+00:00",
        subject="[External] Invoice #5294412 (PO # FPO1131464)",
        internet_message_id="<message@example>",
        subject_po_number="FPO1131464",
        pdf_po_number="FPO1131464",
        vin="1C4SJSBP6SS537975",
        invoice_amount="340.00",
        historical_stage="identity",
        historical_status="blocked",
        historical_code="AMOUNT_MISMATCH",
        historical_detail="Authorized amount mismatch",
        current_po_status="APPROVED",
        current_authorized_amount="340.00",
        already_approved=True,
        amount_match=True,
        assessment_code="ALREADY_APPROVED",
        assessment_detail="",
        moved_to_processed=True,
    )
    return ReviewReportSummary((row,), 1, 1, 0, 0, 0, 1, 0)


def test_main_writes_report_and_prints_summary(monkeypatch, tmp_path, capsys):
    output_path = tmp_path / "invoice_review.csv"
    monkeypatch.setattr(review_cli, "LOCK_PATH", tmp_path / "workflow.lock")
    monkeypatch.setattr(review_cli, "REVIEW_CSV", output_path)
    monkeypatch.setattr(review_cli, "_run_report", _summary)

    exit_code = review_cli.main()

    assert exit_code == 0
    assert output_path.exists()
    output = capsys.readouterr().out
    assert "already_approved=1" in output
    assert "processed_moved=1" in output
    assert "AMOUNT_MISMATCH=1" in output
    assert f"Review CSV: {output_path}" in output


def test_main_returns_one_when_inventory_cannot_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(review_cli, "LOCK_PATH", tmp_path / "workflow.lock")
    monkeypatch.setattr(review_cli, "REVIEW_CSV", tmp_path / "invoice_review.csv")

    def fail():
        raise RuntimeError("Outlook unavailable")

    monkeypatch.setattr(review_cli, "_run_report", fail)

    assert review_cli.main() == 1
    assert "Outlook unavailable" in capsys.readouterr().out


def test_main_stops_before_report_when_csv_is_locked(monkeypatch, tmp_path, capsys):
    output_path = tmp_path / "invoice_review.csv"
    output_path.write_text("existing report", encoding="utf-8")
    run_report = MagicMock()
    monkeypatch.setattr(review_cli, "LOCK_PATH", tmp_path / "workflow.lock")
    monkeypatch.setattr(review_cli, "REVIEW_CSV", output_path)
    monkeypatch.setattr(review_cli, "_run_report", run_report)

    def locked(path):
        raise PermissionError(f"Close the invoice review CSV in Excel: {path}")

    monkeypatch.setattr(review_cli, "_ensure_review_csv_writable", locked)

    assert review_cli.main() == 1
    run_report.assert_not_called()
    assert "Close the invoice review CSV in Excel" in capsys.readouterr().out