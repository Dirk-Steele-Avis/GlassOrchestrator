"""Single command-line composition root for completed invoices."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

from invoice_workflow.coordinator import CompletedInvoicesCoordinator, RunSummary
from invoice_workflow.contracts import OutlookScanResult, ParsedInvoice
from invoice_workflow.identity import StrictIdentityResolver
from invoice_workflow.identity_adapters import (
    FIELDPO_URL,
    FieldPOBrowserIdentityReader,
    GoogleSheetIdentityReader,
)
from invoice_workflow.outlook_source import OutlookInvoiceSource
from invoice_workflow.process_lock import CoordinatorProcessLock
from invoice_workflow.run_logging import WorkflowRunLogger, cleanup_old_run_artifacts
from invoice_workflow.store import InvoiceRepository

ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = ROOT / "log" / "completed_invoices"
DATABASE_PATH = ROOT / "data" / "completed_invoices.db"
LOCK_PATH = ROOT / "data" / "completed_invoices.lock"
ATTACHMENT_ROOT = ROOT / "outlook" / "invoice_attachments"
FIELDPO_PROFILE = ROOT / "outlook" / "browser_profile"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    run_id = uuid4().hex
    cleanup_old_run_artifacts(LOG_ROOT)
    with CoordinatorProcessLock(LOCK_PATH), WorkflowRunLogger(
        LOG_ROOT, run_id
    ) as logger:
        try:
            if args.sweep_terminal_to_needs_review:
                summary = _run_terminal_sweep(args, run_id, logger)
            else:
                summary = _run_live(args, run_id, logger)
        except Exception as exc:
            logger.exception(f"run_failed detail={type(exc).__name__}: {exc}")
            print(f"Completed-invoice live run failed: {type(exc).__name__}: {exc}")
            print(f"Review log: {logger.path}")
            return 1

        if args.sweep_terminal_to_needs_review:
            print("Invoice review sweep")
            print(f"  Moved to Needs Review: {summary.needs_review_moved}")
            print(f"  Move failures: {summary.needs_review_failed}")
        else:
            work_item_results = (
                _load_work_item_results(args.work_item_results)
                if args.work_item_results
                else {}
            )
            _print_live_summary(summary, work_item_results)
        _print_failure_summary(summary)
        quarantine_csv = _write_quarantine_csv(summary, LOG_ROOT)
        if quarantine_csv is not None:
            print(f"Quarantine CSV: {quarantine_csv}")
        print(f"Review log: {logger.path}")
        return summary.exit_code


def _print_live_summary(
    summary: RunSummary,
    work_item_results: dict[tuple[str, str], dict[str, str]] | None = None,
) -> None:
    total_found = summary.captured + summary.capture_failures
    print("Invoice closeout results")
    print(
        f"  Invoices found: {total_found} "
        f"({summary.captured} parsed, {summary.capture_failures} need invoice review)"
    )
    print(
        f"  PO, VIN, MVA, and amount checks: {summary.identities_complete} passed, "
        f"{summary.identities_blocked} need review"
    )
    print(
        f"  Invoice emails: {summary.outlook_moved} moved to Processed, "
        f"{summary.needs_review_moved} moved to Needs Review"
    )
    if summary.outlook_failed or summary.needs_review_failed:
        print(
            f"  Email move failures: Processed={summary.outlook_failed}, "
            f"Needs Review={summary.needs_review_failed}"
        )
    approval_attempted = any(
        invoice.po_status != "pending" for invoice in summary.invoice_outcomes
    )
    if approval_attempted:
        approved = sum(
            invoice.po_status == "complete" for invoice in summary.invoice_outcomes
        )
        approval_issues = sum(
            invoice.po_status in {"failed", "blocked"}
            for invoice in summary.invoice_outcomes
        )
        print(
            f"  FieldPO PO approval: {approved} approved, "
            f"{approval_issues} failed or blocked"
        )
    else:
        print("  PO approval: Not performed by this workflow")
    print("  Complaint creation: Not part of closeout")
    print("  Work-item creation: Not part of closeout; existing items are closed earlier")

    if not summary.invoice_outcomes:
        return
    print("  Per-invoice results:")
    work_item_results = work_item_results or {}
    outlook_failures = {
        failure.invoice_number
        for failure in summary.failures
        if failure.stage == "outlook"
    }
    for invoice in summary.invoice_outcomes:
        invoice_pdf_po = _compare_required(
            invoice.po_number,
            invoice.pdf_po_number,
        )
        invoice_fieldpo_po = _compare_optional(
            invoice.po_number,
            invoice.fieldpo_po_number,
        )
        amount_status = _compare_amount(
            invoice.invoice_amount,
            invoice.authorized_amount,
        )
        vin_status = _compare_optional(invoice.vin, invoice.fieldpo_vin)
        mva_status = _compare_optional(invoice.mva, invoice.fieldpo_mva)

        if invoice.outlook_status == "complete":
            email_status = "Processed"
        elif invoice.invoice_number in outlook_failures:
            email_status = "move failed"
        elif invoice.identity_status in {"blocked", "failed"}:
            email_status = "Needs Review"
        else:
            email_status = "not moved"

        work_item = work_item_results.get((invoice.mva, "Glass"))
        if work_item is None:
            work_item_status = "NOT CHECKED"
        else:
            work_item_status = work_item["status"].replace("_", " ").upper()
            if work_item["detail"]:
                work_item_status += f" ({work_item['detail']})"

        print(
            f"    Invoice #{invoice.invoice_number} | MVA {invoice.mva or 'unknown'}"
        )
        print(
            f"      PO: email {invoice.po_number or 'missing'} / "
            f"PDF {invoice.pdf_po_number or 'missing'} [{invoice_pdf_po}]; "
            f"FieldPO {invoice.fieldpo_po_number or 'not read'} "
            f"[{invoice_fieldpo_po}]"
        )
        print(
            f"      Amount: invoice ${invoice.invoice_amount} / "
            f"authorized {('$' + invoice.authorized_amount) if invoice.authorized_amount else 'not read'} "
            f"[{amount_status}]"
        )
        print(
            f"      VIN: invoice {invoice.vin or 'missing'} / "
            f"FieldPO {invoice.fieldpo_vin or 'not read'} [{vin_status}]"
        )
        print(
            f"      MVA: Sheet {invoice.mva or 'not found'} / "
            f"FieldPO {invoice.fieldpo_mva or 'not read'} [{mva_status}]"
        )
        print(f"      Work item: {work_item_status}")
        print(
            "      FieldPO PO approval: "
            f"{_display_stage_status(invoice.po_status, 'APPROVED')}"
        )
        print(
            "      FieldPO work order: "
            f"{_display_stage_status(invoice.work_order_status, 'CLOSED')}"
        )
        print("      Complaint: NOT CHECKED")
        print(f"      Invoice email: {email_status}")
        if invoice.detail:
            print(f"      Review reason: {invoice.detail}")


def _compare_required(left: str, right: str) -> str:
    if not left or not right:
        return "MISMATCH"
    return "MATCH" if left == right else "MISMATCH"


def _display_stage_status(status: str, completed_label: str) -> str:
    return {
        "complete": completed_label,
        "failed": "FAILED",
        "blocked": "BLOCKED",
        "in_progress": "IN PROGRESS",
        "pending": "NOT CHECKED",
    }.get(status, status.replace("_", " ").upper())


def _compare_optional(left: str, right: str) -> str:
    if not left or not right:
        return "NOT CHECKED"
    return "MATCH" if left == right else "MISMATCH"


def _compare_amount(invoice_amount: str, authorized_amount: str) -> str:
    if not authorized_amount:
        return "NOT CHECKED"
    try:
        invoice_value = Decimal(invoice_amount)
        authorized_value = Decimal(authorized_amount)
    except InvalidOperation:
        return "MISMATCH"
    return "MATCH" if invoice_value == authorized_value else "MISMATCH"


def _load_work_item_results(
    path: str | Path,
) -> dict[tuple[str, str], dict[str, str]]:
    with Path(path).open("r", encoding="utf-8") as result_file:
        payload = json.load(result_file)
    if not isinstance(payload, list):
        raise ValueError("Work-item results must be a JSON list")

    results = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Each work-item result must be a JSON object")
        mva = str(row.get("mva", "")).strip()
        complaint_type = str(row.get("complaint_type", "")).strip()
        status = str(row.get("status", "")).strip()
        if not mva or not complaint_type or not status:
            raise ValueError("Work-item result is missing MVA, complaint type, or status")
        key = (mva, complaint_type)
        if key in results:
            raise ValueError(f"Duplicate work-item result for MVA {mva} ({complaint_type})")
        results[key] = {
            "status": status,
            "detail": str(row.get("detail", "")).strip(),
        }
    return results


def _run_live(
    args: argparse.Namespace,
    run_id: str,
    logger: WorkflowRunLogger,
):
    config = _load_runtime_config()
    account_name = _required_text(config, "account_name")
    spreadsheet_id = _required_text(config, "spreadsheet_id")
    sheet_name = _required_text(config, "sheet_name")
    service_account_json = _resolve_path(
        _required_text(config, "service_account_json")
    )

    pythoncom.CoInitialize()
    try:
        namespace = win32com.client.Dispatch(
            "Outlook.Application"
        ).GetNamespace("MAPI")
        outlook_source = OutlookInvoiceSource(
            namespace,
            account_name=account_name,
            attachment_directory=ATTACHMENT_ROOT / run_id,
        )
        archived_count = outlook_source.archive_expired_processed_invoices()
        logger.info(
            f"processed_invoice_archive_completed moved={archived_count}",
            stage="capture",
        )
        print(
            f"[OUTLOOK] Archived {archived_count} expired Processed invoice(s) "
            "to Invoice_Archive.",
            flush=True,
        )
        repository = InvoiceRepository(DATABASE_PATH)
        captured_scan = None
        closeout_reviews: tuple[tuple[ParsedInvoice, str], ...] = ()
        if args.close_work_items:
            repository.initialize()
            terminal_message_ids = frozenset(
                repository.list_terminal_identity_message_ids()
            )
            captured_scan = outlook_source.scan_parent_invoices(
                invoice_number=args.invoice_number,
                max_invoices=args.max_invoices,
                excluded_message_ids=(
                    frozenset()
                    if args.invoice_number is not None
                    else terminal_message_ids
                ),
            )
            for invoice in captured_scan.invoices:
                if invoice.internet_message_id in terminal_message_ids:
                    raise ValueError(
                        f"Selected Invoice #{invoice.invoice_number} is already terminal "
                        "by exact InternetMessageID; closeout was not started"
                    )
            reviews, deferred = _close_selected_work_items(
                captured_scan, config, args.work_item_results, logger,
            )
            closeout_reviews = tuple(reviews)
            removed = deferred | {invoice.internet_message_id for invoice, _ in reviews}
            captured_scan = OutlookScanResult(
                tuple(
                    invoice for invoice in captured_scan.invoices
                    if invoice.internet_message_id not in removed
                ),
                captured_scan.failures,
            )
        sheet_reader = GoogleSheetIdentityReader(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            service_account_json=service_account_json,
        )

        with ExitStack() as stack:
            playwright = stack.enter_context(sync_playwright())
            browser = playwright.chromium.launch_persistent_context(
                str(FIELDPO_PROFILE),
                headless=False,
                args=["--start-maximized"],
                no_viewport=True,
            )
            stack.callback(browser.close)
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(FIELDPO_URL)
            page.wait_for_url("**/fieldpo/dashboard**", timeout=120_000)
            fieldpo_identity_reader = FieldPOBrowserIdentityReader(page)
            resolver = StrictIdentityResolver(sheet_reader, fieldpo_identity_reader)
            fieldpo_approver = None
            if captured_scan is not None:
                from invoice_workflow.identity_adapters import FieldPOBrowserApprover

                fieldpo_approver = FieldPOBrowserApprover(
                    page, fieldpo_identity_reader,
                )
            coordinator = CompletedInvoicesCoordinator(
                outlook_source,
                resolver,
                repository,
                logger,
                run_id=run_id,
            )
            if captured_scan is not None:
                return coordinator.run_captured(
                    captured_scan, invoice_number=args.invoice_number,
                    closeout_reviews=closeout_reviews,
                    fieldpo_approver=fieldpo_approver,
                )
            return coordinator.run_live(
                invoice_number=args.invoice_number,
                max_invoices=args.max_invoices,
            )
    finally:
        pythoncom.CoUninitialize()


def _close_selected_work_items(
    scan: OutlookScanResult,
    config: dict[str, Any],
    results_path: Path,
    logger: WorkflowRunLogger,
) -> tuple[list[tuple[ParsedInvoice, str]], set[str]]:
    """Close work items for the captured selection.

    Returns invoices that need review and message IDs deferred to a later run.
    Per-invoice problems are returned for Needs Review; system failures raise.
    """
    from WorkItems.build_close_queue import prepare_selected_closeout
    from vendor_tracking.sheet_updater import VendorSheetUpdater

    for failure in scan.failures:
        print(f"[CLOSEOUT] Needs Review: {failure.subject}: {failure.detail}")
    print(f"[CLOSEOUT] Selected {len(scan.invoices)} invoice(s) for every closeout stage.")
    for invoice in scan.invoices:
        print(f"[CLOSEOUT] Selected Invoice #{invoice.invoice_number} | {invoice.subject}")
        logger.info(
            f"invoice_selected internet_message_id={invoice.internet_message_id}",
            invoice=invoice.invoice_number,
            stage="capture",
        )
    invoices = [
        {
            "subject": invoice.subject,
            "vin": invoice.vin,
            "invoice_amount": str(invoice.invoice_amount),
            "received": invoice.received_at.isoformat(),
            "is_recalibration": invoice.is_recalibration,
            "selection_index": index,
        }
        for index, invoice in enumerate(scan.invoices)
    ]
    reviews: list[tuple[ParsedInvoice, str]] = []
    deferred: set[str] = set()
    targets = []
    if invoices:
        updater = VendorSheetUpdater(
            _required_text(config, "spreadsheet_id"),
            _required_text(config, "sheet_name"),
            str(_resolve_path(_required_text(config, "service_account_json"))),
        )
        updater.connect()
        targets, sheet_reviews = prepare_selected_closeout(updater, invoices)
        reviews.extend(
            (scan.invoices[invoice["selection_index"]], f"Closeout Sheet check failed: {note}")
            for invoice, note in sheet_reviews
        )

    if not targets:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text("[]", encoding="utf-8")
        logger.info("no_selected_work_item_targets", stage="compass")
        return reviews, deferred

    logger.info(f"selected_work_items_started count={len(targets)}", stage="compass")
    results_path.unlink(missing_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "WorkItems" / "close_workitem.py"),
            "--only-mvas", ",".join(target["mva"] for target in targets),
            "--concise-console",
            "--results-json", str(results_path),
        ],
        cwd=ROOT,
        check=False,
    )
    if not results_path.exists():
        raise RuntimeError(
            f"Selected work-item closure failed with exit code {result.returncode} "
            "and wrote no results; invoice validation was not started"
        )
    work_item_results = _load_work_item_results(results_path)
    expected = {(target["mva"], target["complaint_type"]) for target in targets}
    if work_item_results.keys() != expected:
        raise ValueError("Selected work-item results do not match the captured invoice targets")
    failed_outcomes = 0
    for target in targets:
        mva, complaint_type = target["mva"], target["complaint_type"]
        outcome = work_item_results[(mva, complaint_type)]
        invoice = scan.invoices[target["selection_index"]]
        if outcome["status"] in {"closed", "not_found"}:
            continue
        if outcome["status"] == "not_attempted":
            deferred.add(invoice.internet_message_id)
            print(
                f"[CLOSEOUT] Deferred Invoice #{invoice.invoice_number} | MVA {mva}: "
                f"{outcome['detail']}; left for the next run"
            )
            continue
        failed_outcomes += 1
        detail = (
            f"Work-item closure {outcome['status']} for MVA {mva} ({complaint_type})"
            + (f": {outcome['detail']}" if outcome["detail"] else "")
        )
        reviews.append((invoice, detail))
        print(f"[CLOSEOUT] Needs Review: Invoice #{invoice.invoice_number}: {detail}")
    if result.returncode and not failed_outcomes:
        raise RuntimeError(
            f"Selected work-item closure exited {result.returncode} without a failed "
            "work-item result; invoice validation was not started"
        )
    logger.info(
        f"selected_work_items_completed count={len(targets)} "
        f"review={failed_outcomes} deferred={len(deferred)}",
        stage="compass",
    )
    return reviews, deferred


def _run_terminal_sweep(
    args: argparse.Namespace,
    run_id: str,
    logger: WorkflowRunLogger,
):
    if args.invoice_number is not None:
        raise ValueError(
            "--invoice-number cannot be used with --sweep-terminal-to-needs-review"
        )

    config = _load_runtime_config()
    account_name = _required_text(config, "account_name")
    pythoncom.CoInitialize()
    try:
        namespace = win32com.client.Dispatch(
            "Outlook.Application"
        ).GetNamespace("MAPI")
        outlook_source = OutlookInvoiceSource(
            namespace,
            account_name=account_name,
            attachment_directory=ATTACHMENT_ROOT / run_id,
        )
        class _NoopIdentityResolver:
            def resolve(self, invoice):
                raise RuntimeError("identity resolution is not used in sweep mode")

        coordinator = CompletedInvoicesCoordinator(
            outlook_source,
            _NoopIdentityResolver(),
            InvoiceRepository(DATABASE_PATH),
            logger,
            run_id=run_id,
        )
        return coordinator.run_sweep_terminal_to_needs_review(
            max_invoices=args.max_invoices,
        )
    finally:
        pythoncom.CoUninitialize()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process completed AGN invoices")
    parser.add_argument("--max-invoices", type=_positive_int)
    parser.add_argument("--invoice-number", type=_invoice_number)
    parser.add_argument("--sweep-terminal-to-needs-review", action="store_true")
    parser.add_argument("--work-item-results", type=Path)
    parser.add_argument("--close-work-items", action="store_true")
    args = parser.parse_args(argv)
    if args.close_work_items:
        if args.max_invoices is None and args.invoice_number is None:
            parser.error("--close-work-items requires --max-invoices or --invoice-number")
        if args.sweep_terminal_to_needs_review:
            parser.error("--close-work-items cannot be combined with --sweep-terminal-to-needs-review")
        if args.work_item_results is None:
            parser.error("--close-work-items requires --work-item-results")
    return args


def _load_runtime_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in (
        ROOT / "orchestrator_config.json",
        ROOT / "orchestrator_project.json",
        ROOT / "orchestrator_project.local.json",
        ROOT / "outlook" / "agn_invoices_config.json",
    ):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as config_file:
            payload = json.load(config_file)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected JSON object in {path}")
        config.update(payload)
    return config


def _required_text(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key, "")).strip()
    if not value or value == "YOUR_SPREADSHEET_ID_HERE":
        raise ValueError(f"Missing completed-invoice config value: {key}")
    return value


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _invoice_number(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("must contain only digits")
    return value


def _print_failure_summary(summary: RunSummary) -> None:
    if not summary.failures:
        return
    print("Failure summary:")
    for failure in summary.failures:
        print(
            f"  invoice={failure.invoice_number} stage={failure.stage} "
            f"code={failure.code} detail={failure.detail}"
        )


def _write_quarantine_csv(summary: RunSummary, log_root: Path) -> Path | None:
    if not summary.failures:
        return None
    directory = log_root / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{summary.run_id}.csv"
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "run_id",
                "invoice_number",
                "stage",
                "code",
                "detail",
                "subject",
                "subject_po_number",
                "pdf_po_number",
                "vin",
                "invoice_amount",
            ]
        )
        for failure in summary.failures:
            writer.writerow(
                [
                    failure.run_id,
                    failure.invoice_number,
                    failure.stage,
                    failure.code,
                    failure.detail,
                    failure.subject,
                    failure.subject_po_number,
                    failure.pdf_po_number,
                    failure.vin,
                    failure.invoice_amount,
                ]
            )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
