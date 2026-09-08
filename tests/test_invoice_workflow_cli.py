from pathlib import Path

from invoice_workflow.cli import _write_quarantine_csv
from invoice_workflow.coordinator import FailureRecord, RunSummary


def _summary_with_failure() -> RunSummary:
    return RunSummary(
        run_id="run-123",
        captured=1,
        capture_failures=0,
        identities_complete=0,
        identities_blocked=1,
        outlook_moved=0,
        outlook_failed=0,
        needs_review_moved=1,
        needs_review_failed=0,
        failures=(
            FailureRecord(
                run_id="run-123",
                invoice_number="5283680",
                stage="identity",
                code="PO_FORMAT_INVALID",
                detail="Invoice subject does not contain one exact PO number",
                subject="[External] Invoice #5283680 (PO # FPO1128576-)",
                subject_po_number="",
                pdf_po_number="FPO1128576",
                vin="1HGCY1F49TA029843",
                invoice_amount="340.00",
            ),
        ),
        exit_code=2,
    )


def test_write_quarantine_csv_writes_expected_columns(tmp_path: Path):
    summary = _summary_with_failure()

    path = _write_quarantine_csv(summary, tmp_path)

    assert path == tmp_path / "quarantine" / "run-123.csv"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "run_id,invoice_number,stage,code,detail,subject,"
        "subject_po_number,pdf_po_number,vin,invoice_amount"
    )
    assert "PO_FORMAT_INVALID" in lines[1]
    assert "5283680" in lines[1]


def test_write_quarantine_csv_skips_when_no_failures(tmp_path: Path):
    summary = RunSummary(
        run_id="run-123",
        captured=1,
        capture_failures=0,
        identities_complete=1,
        identities_blocked=0,
        outlook_moved=1,
        outlook_failed=0,
        needs_review_moved=0,
        needs_review_failed=0,
        failures=(),
        exit_code=0,
    )

    path = _write_quarantine_csv(summary, tmp_path)

    assert path is None
