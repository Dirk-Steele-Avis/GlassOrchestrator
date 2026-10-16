import json
from pathlib import Path

from invoice_workflow.cli import (
    _load_work_item_results,
    _print_live_summary,
    _write_quarantine_csv,
)
from invoice_workflow.coordinator import (
    FailureRecord,
    InvoiceRunOutcome,
    RunSummary,
)


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


def test_live_summary_is_readable_and_reports_not_performed_stages(capsys):
    summary = RunSummary(
        run_id="run-123",
        captured=1,
        capture_failures=1,
        identities_complete=1,
        identities_blocked=0,
        outlook_moved=1,
        outlook_failed=0,
        needs_review_moved=1,
        needs_review_failed=0,
        failures=(),
        exit_code=2,
        invoice_outcomes=(
            InvoiceRunOutcome(
                invoice_number="5463789",
                po_number="FPO1131464",
                pdf_po_number="FPO1131464",
                fieldpo_po_number="FPO1131464",
                vin="5XYP6DGC3SG682826",
                fieldpo_vin="5XYP6DGC3SG682826",
                mva="058889320",
                fieldpo_mva="058889320",
                invoice_amount="340.00",
                authorized_amount="340.00",
                identity_status="complete",
                outlook_status="complete",
                detail="",
            ),
        ),
    )

    _print_live_summary(
        summary,
        {
            ("058889320", "Glass"): {
                "status": "closed",
                "detail": "GLASS-GLASS",
            },
        },
    )

    output = capsys.readouterr().out
    assert "Invoices found: 2 (1 parsed, 1 need invoice review)" in output
    assert "PO, VIN, MVA, and amount checks: 1 passed, 0 need review" in output
    assert "Invoice #5463789 | MVA 058889320" in output
    assert "PO: email FPO1131464 / PDF FPO1131464 [MATCH]" in output
    assert "FieldPO FPO1131464 [MATCH]" in output
    assert "Amount: invoice $340.00 / authorized $340.00 [MATCH]" in output
    assert "VIN: invoice 5XYP6DGC3SG682826 / FieldPO 5XYP6DGC3SG682826 [MATCH]" in output
    assert "MVA: Sheet 058889320 / FieldPO 058889320 [MATCH]" in output
    assert "Work item: CLOSED (GLASS-GLASS)" in output
    assert "PO approval: Not performed by this workflow" in output
    assert "Complaint creation: Not part of closeout" in output


def test_live_summary_shows_each_unexpected_fieldpo_value(capsys):
    summary = RunSummary(
        run_id="run-456",
        captured=1,
        capture_failures=0,
        identities_complete=0,
        identities_blocked=1,
        outlook_moved=0,
        outlook_failed=0,
        needs_review_moved=1,
        needs_review_failed=0,
        failures=(),
        exit_code=2,
        invoice_outcomes=(
            InvoiceRunOutcome(
                invoice_number="5464000",
                po_number="FPO1131464",
                pdf_po_number="FPO1131464",
                fieldpo_po_number="FPO1131465",
                vin="5XYP6DGC3SG682826",
                fieldpo_vin="1HGCY1F49TA029843",
                mva="058889320",
                fieldpo_mva="058889321",
                invoice_amount="340.00",
                authorized_amount="400.00",
                identity_status="blocked",
                outlook_status="pending",
                detail="FieldPO PO mismatch",
            ),
        ),
    )

    _print_live_summary(summary)

    output = capsys.readouterr().out
    assert "email FPO1131464 / PDF FPO1131464 [MATCH]" in output
    assert "FieldPO FPO1131465 [MISMATCH]" in output
    assert "authorized $400.00 [MISMATCH]" in output
    assert "FieldPO 1HGCY1F49TA029843 [MISMATCH]" in output
    assert "FieldPO 058889321 [MISMATCH]" in output
    assert "Work item: NOT CHECKED" in output


def test_load_work_item_results_uses_exact_mva_and_complaint_type(tmp_path: Path):
    path = tmp_path / "work-items.json"
    path.write_text(
        json.dumps([
            {
                "mva": "058889320",
                "complaint_type": "Glass",
                "status": "closed",
                "detail": "",
            },
        ]),
        encoding="utf-8",
    )

    assert _load_work_item_results(path) == {
        ("058889320", "Glass"): {"status": "closed", "detail": ""},
    }


def test_load_work_item_results_rejects_duplicate_keys(tmp_path: Path):
    path = tmp_path / "duplicate-work-items.json"
    path.write_text(
        json.dumps([
            {"mva": "058889320", "complaint_type": "Glass", "status": "closed"},
            {"mva": "058889320", "complaint_type": "Glass", "status": "not_found"},
        ]),
        encoding="utf-8",
    )

    try:
        _load_work_item_results(path)
    except ValueError as exc:
        assert "Duplicate work-item result" in str(exc)
    else:
        raise AssertionError("duplicate work-item results must be rejected")
