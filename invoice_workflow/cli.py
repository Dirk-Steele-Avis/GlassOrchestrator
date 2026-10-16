"""Single command-line composition root for completed invoices."""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

from invoice_workflow.coordinator import CompletedInvoicesCoordinator, RunSummary
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
        print("      PO approval: NOT CHECKED")
        print("      Complaint: NOT CHECKED")
        print(f"      Invoice email: {email_status}")
        if invoice.detail:
            print(f"      Review reason: {invoice.detail}")


def _compare_required(left: str, right: str) -> str:
    if not left or not right:
        return "MISMATCH"
    return "MATCH" if left == right else "MISMATCH"


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
            resolver = StrictIdentityResolver(
                sheet_reader,
                FieldPOBrowserIdentityReader(page),
            )
            coordinator = CompletedInvoicesCoordinator(
                outlook_source,
                resolver,
                InvoiceRepository(DATABASE_PATH),
                logger,
                run_id=run_id,
            )
            return coordinator.run_live(
                invoice_number=args.invoice_number,
                max_invoices=args.max_invoices,
            )
    finally:
        pythoncom.CoUninitialize()


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
    return parser.parse_args(argv)


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
