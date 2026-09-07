"""Single command-line composition root for completed invoices."""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import ExitStack
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

        print(
            "Completed-invoice live run: "
            f"captured={summary.captured}, "
            f"capture_failures={summary.capture_failures}, "
            f"identity_complete={summary.identities_complete}, "
            f"identity_blocked={summary.identities_blocked}, "
            f"outlook_moved={summary.outlook_moved}, "
            f"outlook_failed={summary.outlook_failed}, "
            f"needs_review_moved={summary.needs_review_moved}, "
            f"needs_review_failed={summary.needs_review_failed}"
        )
        _print_failure_summary(summary)
        quarantine_csv = _write_quarantine_csv(summary, LOG_ROOT)
        if quarantine_csv is not None:
            print(f"Quarantine CSV: {quarantine_csv}")
        print(f"Review log: {logger.path}")
        return summary.exit_code


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
    parser.add_argument("--resume-failed", action="store_true")
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
