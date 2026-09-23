"""Composition root for reviewing invoices and finalizing approved email."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

from invoice_workflow.cli import (
    ATTACHMENT_ROOT,
    DATABASE_PATH,
    FIELDPO_PROFILE,
    LOCK_PATH,
    ROOT,
    _load_runtime_config,
    _required_text,
)
from invoice_workflow.identity_adapters import (
    FIELDPO_URL,
    FieldPOBrowserReviewReader,
)
from invoice_workflow.outlook_source import OutlookInvoiceSource
from invoice_workflow.process_lock import CoordinatorProcessLock
from invoice_workflow.review_report import (
    ReviewReportSummary,
    build_review_report,
    move_approved_to_processed,
    write_review_csv,
)
from invoice_workflow.store import InvoiceRepository

REVIEW_CSV = ROOT / "outlook" / "invoice_review.csv"


def main() -> int:
    try:
        with CoordinatorProcessLock(LOCK_PATH):
            _ensure_review_csv_writable(REVIEW_CSV)
            summary = _run_report()
    except Exception as exc:
        print(f"Invoice review report failed: {type(exc).__name__}: {exc}")
        return 1
    write_review_csv(summary.rows, REVIEW_CSV)
    _print_summary(summary, REVIEW_CSV)
    return summary.exit_code


def _ensure_review_csv_writable(path: Path) -> None:
    """Fail before external reads or moves when Excel has locked the report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with path.open("r+b"):
                pass
        except PermissionError as exc:
            raise PermissionError(
                f"Close the invoice review CSV in Excel before running: {path}"
            ) from exc
        return
    probe_path = path.with_suffix(path.suffix + ".write-test")
    try:
        with probe_path.open("xb"):
            pass
    finally:
        probe_path.unlink(missing_ok=True)


def _run_report() -> ReviewReportSummary:
    config = _load_runtime_config()
    account_name = _required_text(config, "account_name")
    run_id = uuid4().hex
    pythoncom.CoInitialize()
    try:
        namespace = win32com.client.Dispatch(
            "Outlook.Application"
        ).GetNamespace("MAPI")
        outlook_source = OutlookInvoiceSource(
            namespace,
            account_name=account_name,
            attachment_directory=ATTACHMENT_ROOT / "review" / run_id,
        )
        scan = outlook_source.scan_needs_review_invoices()
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
            summary = build_review_report(
                scan,
                InvoiceRepository(DATABASE_PATH),
                FieldPOBrowserReviewReader(page),
            )
            return move_approved_to_processed(summary, outlook_source)
    finally:
        pythoncom.CoUninitialize()


def _print_summary(summary: ReviewReportSummary, output_path: Path) -> None:
    print(
        "Invoice review report: "
        f"total={len(summary.rows)}, "
        f"already_approved={summary.already_approved}, "
        f"amount_matches={summary.amount_matches}, "
        f"amount_mismatches={summary.amount_mismatches}, "
        f"history_not_found={summary.history_not_found}, "
        f"diagnostic_failures={summary.diagnostic_failures}, "
        f"processed_moved={summary.processed_moved}, "
        f"processed_move_failures={summary.processed_move_failures}"
    )
    reason_counts = Counter(row.historical_code for row in summary.rows)
    if reason_counts:
        print(
            "Original reasons: "
            + ", ".join(
                f"{code}={count}" for code, count in sorted(reason_counts.items())
            )
        )
    print(f"Review CSV: {output_path}")


if __name__ == "__main__":
    raise SystemExit(main())