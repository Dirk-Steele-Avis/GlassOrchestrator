import json
from pathlib import Path
from datetime import UTC, datetime
from decimal import Decimal
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from invoice_workflow import cli
from invoice_workflow.contracts import CaptureFailure, OutlookScanResult, ParsedInvoice
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


def _selected_invoice():
    return ParsedInvoice(
        internet_message_id="<selected@example>",
        entry_id="selected-entry",
        invoice_number="5485994",
        subject="[External] Invoice #5485994 (PO # FPO1178730)",
        received_at=datetime(2026, 10, 22, tzinfo=UTC),
        subject_po_number="FPO1178730",
        pdf_po_number="FPO1178730",
        vin="5XYP64GC1SG678709",
        invoice_amount=Decimal("340.00"),
        pdf_path="selected.pdf",
    )


@pytest.mark.parametrize("limit", ["0", "-1", "abc"])
def test_scoped_limit_rejects_invalid_counts(limit):
    with pytest.raises(SystemExit) as error:
        cli._parse_args([
            "--close-work-items", "--max-invoices", limit,
            "--work-item-results", "results.json",
        ])
    assert error.value.code == 2


def _mock_scoped_runtime(monkeypatch, tmp_path):
    config = {
        "account_name": "test@example", "spreadsheet_id": "sheet",
        "sheet_name": "GlassClaims", "service_account_json": "service.json",
    }
    monkeypatch.setattr(cli, "_load_runtime_config", lambda: config)
    monkeypatch.setattr(cli.pythoncom, "CoInitialize", MagicMock())
    monkeypatch.setattr(cli.pythoncom, "CoUninitialize", MagicMock())
    monkeypatch.setattr(cli.win32com.client, "Dispatch", MagicMock())
    source = MagicMock()
    source.scan_parent_invoices.return_value = OutlookScanResult((_selected_invoice(),), ())
    monkeypatch.setattr(cli, "OutlookInvoiceSource", MagicMock(return_value=source))
    repository = MagicMock()
    repository.list_terminal_identity_message_ids.return_value = ("<terminal@example>",)
    monkeypatch.setattr(cli, "InvoiceRepository", MagicMock(return_value=repository))
    monkeypatch.setattr(cli, "GoogleSheetIdentityReader", MagicMock())
    monkeypatch.setattr(cli, "StrictIdentityResolver", MagicMock())
    playwright = MagicMock()
    monkeypatch.setattr(cli, "sync_playwright", playwright)
    coordinator = MagicMock()
    monkeypatch.setattr(cli, "CompletedInvoicesCoordinator", MagicMock(return_value=coordinator))
    args = cli._parse_args([
        "--close-work-items", "--max-invoices", "1",
        "--work-item-results", str(tmp_path / "results.json"),
    ])
    return args, source, coordinator, playwright


def test_scoped_closeout_captures_once_and_reuses_exact_selection(monkeypatch, tmp_path):
    from invoice_workflow.identity_adapters import FieldPOBrowserApprover

    args, source, coordinator, playwright = _mock_scoped_runtime(monkeypatch, tmp_path)
    close_items = MagicMock(return_value=([], set()))
    monkeypatch.setattr(cli, "_close_selected_work_items", close_items)

    result = cli._run_live(args, "test-run", MagicMock())

    scan = source.scan_parent_invoices.return_value
    source.archive_expired_processed_invoices.assert_called_once_with()
    source.scan_parent_invoices.assert_called_once_with(
        invoice_number=None, max_invoices=1,
        excluded_message_ids=frozenset({"<terminal@example>"}),
    )
    assert close_items.call_args.args[0] is scan
    coordinator.run_captured.assert_called_once_with(
        scan,
        invoice_number=None,
        closeout_reviews=(),
        fieldpo_approver=coordinator.run_captured.call_args.kwargs["fieldpo_approver"],
    )
    assert isinstance(
        coordinator.run_captured.call_args.kwargs["fieldpo_approver"],
        FieldPOBrowserApprover,
    )
    coordinator.run_live.assert_not_called()
    assert result is coordinator.run_captured.return_value
    playwright.assert_called_once()


def test_archive_failure_stops_before_invoice_search(monkeypatch, tmp_path):
    args, source, coordinator, playwright = _mock_scoped_runtime(monkeypatch, tmp_path)
    source.archive_expired_processed_invoices.side_effect = RuntimeError(
        "SentOn is missing"
    )

    with pytest.raises(RuntimeError, match="SentOn is missing"):
        cli._run_live(args, "test-run", MagicMock())

    source.scan_parent_invoices.assert_not_called()
    coordinator.run_captured.assert_not_called()
    coordinator.run_live.assert_not_called()
    playwright.assert_not_called()


def test_scoped_closeout_routes_reviewed_and_deferred_invoices(monkeypatch, tmp_path):
    from invoice_workflow.identity_adapters import FieldPOBrowserApprover

    args, source, coordinator, playwright = _mock_scoped_runtime(monkeypatch, tmp_path)
    reviewed = _selected_invoice()
    deferred = replace(reviewed, internet_message_id="<deferred@example>", invoice_number="2")
    kept = replace(reviewed, internet_message_id="<kept@example>", invoice_number="3")
    failure = CaptureFailure("entry", "Invoice #4", "recalibration in-house", "<failed@example>")
    source.scan_parent_invoices.return_value = OutlookScanResult(
        (reviewed, deferred, kept), (failure,),
    )
    review = (reviewed, "Work-item closure error for MVA 059235573 (Glass)")
    monkeypatch.setattr(
        cli, "_close_selected_work_items",
        MagicMock(return_value=([review], {"<deferred@example>"})),
    )

    cli._run_live(args, "test-run", MagicMock())

    coordinator.run_captured.assert_called_once_with(
        OutlookScanResult((kept,), (failure,)),
        invoice_number=None,
        closeout_reviews=(review,),
        fieldpo_approver=coordinator.run_captured.call_args.kwargs["fieldpo_approver"],
    )
    assert isinstance(
        coordinator.run_captured.call_args.kwargs["fieldpo_approver"],
        FieldPOBrowserApprover,
    )


def test_scoped_closeout_stops_before_invoice_stage_on_work_item_failure(monkeypatch, tmp_path):
    args, source, coordinator, playwright = _mock_scoped_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli, "_close_selected_work_items",
        MagicMock(side_effect=RuntimeError("selected closure failed")),
    )

    with pytest.raises(RuntimeError, match="selected closure failed"):
        cli._run_live(args, "test-run", MagicMock())
    source.scan_parent_invoices.assert_called_once()
    playwright.assert_not_called()
    coordinator.run_captured.assert_not_called()
    cli.pythoncom.CoUninitialize.assert_called_once()


def test_explicit_terminal_invoice_stops_before_closeout_mutations(monkeypatch, tmp_path):
    args, source, coordinator, playwright = _mock_scoped_runtime(monkeypatch, tmp_path)
    args.invoice_number = "5485994"
    repository = cli.InvoiceRepository.return_value
    repository.list_terminal_identity_message_ids.return_value = ("<selected@example>",)
    close_items = MagicMock()
    monkeypatch.setattr(cli, "_close_selected_work_items", close_items)

    with pytest.raises(ValueError, match="already terminal"):
        cli._run_live(args, "test-run", MagicMock())
    close_items.assert_not_called()
    playwright.assert_not_called()


def test_scoped_capture_failure_is_reviewed_without_stopping(monkeypatch, tmp_path):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    updater = MagicMock()
    prepare = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", updater)
    monkeypatch.setattr(build_close_queue, "prepare_selected_closeout", prepare)
    monkeypatch.setattr(cli.subprocess, "run", run)
    scan = OutlookScanResult(
        (), (CaptureFailure("entry", "Invoice #5485994", "missing PDF"),),
    )
    assert cli._close_selected_work_items(
        scan, {}, tmp_path / "results.json", MagicMock(),
    ) == ([], set())
    updater.assert_not_called()
    prepare.assert_not_called()
    run.assert_not_called()


def test_scoped_sheet_failure_returns_that_invoice_for_review(monkeypatch, tmp_path):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", MagicMock())

    def prepare(_updater, invoices):
        return [], [(invoices[0], "VIN not found")]

    monkeypatch.setattr(build_close_queue, "prepare_selected_closeout", prepare)
    run = MagicMock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    reviews, deferred = cli._close_selected_work_items(
        OutlookScanResult((_selected_invoice(),), ()), config,
        tmp_path / "results.json", MagicMock(),
    )
    assert reviews == [(_selected_invoice(), "Closeout Sheet check failed: VIN not found")]
    assert deferred == set()
    run.assert_not_called()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_scoped_work_item_command_cannot_consume_unrelated_queue(monkeypatch, tmp_path, exit_code):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", MagicMock())
    prepare = MagicMock(return_value=(
        [{"mva": "059235573", "complaint_type": "Glass", "selection_index": 0}], [],
    ))
    monkeypatch.setattr(build_close_queue, "prepare_selected_closeout", prepare)
    path = tmp_path / "results.json"
    path.write_text('["stale results"]', encoding="utf-8")

    def run(command, **kwargs):
        assert not path.exists()
        assert command[command.index("--only-mvas") + 1] == "059235573"
        if exit_code == 0:
            path.write_text(json.dumps([{
                "mva": "059235573", "complaint_type": "Glass",
                "status": "closed", "detail": "",
            }]), encoding="utf-8")
        return type("Result", (), {"returncode": exit_code})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    scan = OutlookScanResult((_selected_invoice(),), ())
    if exit_code:
        with pytest.raises(RuntimeError, match="exit code 7 and wrote no results"):
            cli._close_selected_work_items(scan, config, path, MagicMock())
    else:
        cli._close_selected_work_items(scan, config, path, MagicMock())
    assert len(prepare.call_args.args[1]) == 1
    assert prepare.call_args.args[1][0]["vin"] == _selected_invoice().vin


@pytest.mark.parametrize("outcome", ["missing", "wrong_mva", "exit_without_failure"])
def test_scoped_work_item_output_must_confirm_exact_targets(monkeypatch, tmp_path, outcome):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", MagicMock())
    monkeypatch.setattr(
        build_close_queue, "prepare_selected_closeout",
        MagicMock(return_value=(
            [{"mva": "059235573", "complaint_type": "Glass", "selection_index": 0}], [],
        )),
    )
    path = tmp_path / "results.json"

    def run(*args, **kwargs):
        if outcome != "missing":
            path.write_text(json.dumps([{
                "mva": "011111111" if outcome == "wrong_mva" else "059235573",
                "complaint_type": "Glass",
                "status": "closed",
                "detail": "",
            }]), encoding="utf-8")
        return type("Result", (), {"returncode": 1 if outcome != "wrong_mva" else 0})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    with pytest.raises((RuntimeError, ValueError)):
        cli._close_selected_work_items(
            OutlookScanResult((_selected_invoice(),), ()), config, path, MagicMock(),
        )


def test_scoped_work_item_failures_are_reviewed_and_unattempted_are_deferred(monkeypatch, tmp_path):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    first = _selected_invoice()
    second = replace(first, internet_message_id="<second@example>", invoice_number="2")
    third = replace(first, internet_message_id="<third@example>", invoice_number="3")
    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", MagicMock())
    monkeypatch.setattr(
        build_close_queue, "prepare_selected_closeout",
        MagicMock(return_value=([
            {"mva": "011111111", "complaint_type": "Glass", "selection_index": 0},
            {"mva": "022222222", "complaint_type": "Glass", "selection_index": 1},
            {"mva": "033333333", "complaint_type": "Glass", "selection_index": 2},
        ], [])),
    )
    path = tmp_path / "results.json"

    def run(*args, **kwargs):
        path.write_text(json.dumps([
            {"mva": "011111111", "complaint_type": "Glass", "status": "closed", "detail": ""},
            {"mva": "022222222", "complaint_type": "Glass", "status": "nav_failed", "detail": "Search timeout"},
            {"mva": "033333333", "complaint_type": "Glass", "status": "not_attempted",
             "detail": "batch stopped after navigation failure for 022222222"},
        ]), encoding="utf-8")
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    reviews, deferred = cli._close_selected_work_items(
        OutlookScanResult((first, second, third), ()), config, path, MagicMock(),
    )
    assert reviews == [(
        second, "Work-item closure nav_failed for MVA 022222222 (Glass): Search timeout",
    )]
    assert deferred == {"<third@example>"}

def test_recalibration_keeps_selected_invoice_without_another_work_item(monkeypatch, tmp_path):
    from WorkItems import build_close_queue
    from vendor_tracking import sheet_updater

    monkeypatch.setattr(sheet_updater, "VendorSheetUpdater", MagicMock())
    prepare = MagicMock(return_value=([], []))
    monkeypatch.setattr(build_close_queue, "prepare_selected_closeout", prepare)
    run = MagicMock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    path = tmp_path / "results.json"
    path.write_text('["stale"]', encoding="utf-8")
    invoice = replace(_selected_invoice(), is_recalibration=True)
    config = {
        "spreadsheet_id": "sheet", "sheet_name": "GlassClaims",
        "service_account_json": "service.json",
    }
    cli._close_selected_work_items(
        OutlookScanResult((invoice,), ()), config, path, MagicMock(),
    )
    assert prepare.call_args.args[1][0]["is_recalibration"] is True
    run.assert_not_called()
    assert json.loads(path.read_text(encoding="utf-8")) == []


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

    approved_invoice = replace(
        summary.invoice_outcomes[0],
        po_status="complete",
        work_order_status="complete",
    )
    _print_live_summary(
        replace(summary, invoice_outcomes=(approved_invoice,)),
    )
    approved_output = capsys.readouterr().out
    assert "FieldPO PO approval: 1 approved, 0 failed or blocked" in approved_output
    assert "FieldPO PO approval: APPROVED" in approved_output
    assert "FieldPO work order: CLOSED" in approved_output


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
