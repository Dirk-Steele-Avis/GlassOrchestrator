"""Read-only audit of invoices currently in Outlook Needs Review."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

from invoice_workflow.cli import (
    FIELDPO_PROFILE,
    _load_runtime_config,
    _required_text,
    _resolve_path,
)
from invoice_workflow.contracts import (
    FieldPOReviewReader,
    FieldPOReviewSnapshot,
    IdentityResolver,
    IdentityResult,
    OutlookScanResult,
    ParsedInvoice,
    StageStatus,
)
from invoice_workflow.identity import StrictIdentityResolver
from invoice_workflow.identity_adapters import (
    FIELDPO_URL,
    FieldPOBrowserIdentityReader,
    FieldPOBrowserReviewReader,
    GoogleSheetIdentityReader,
)
from invoice_workflow.outlook_source import OutlookInvoiceSource
from invoice_workflow.store import InvoiceRepository, InvoiceState

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class InvoiceReviewAudit:
    invoice: ParsedInvoice
    identity_result: IdentityResult
    fieldpo_review: FieldPOReviewSnapshot | None
    ledger_state: InvoiceState | None
    blockers: tuple[str, ...]

    @property
    def candidate(self) -> bool:
        return not self.blockers


def audit_invoice(
    invoice: ParsedInvoice,
    resolver: IdentityResolver,
    review_reader: FieldPOReviewReader,
    repository: InvoiceRepository,
) -> InvoiceReviewAudit:
    identity_result = resolver.resolve(invoice)
    identity = identity_result.identity
    if (
        identity_result.status is not StageStatus.COMPLETE
        or identity is None
    ):
        detail = identity_result.detail or (
            f"Identity checks returned {identity_result.status}"
        )
        return InvoiceReviewAudit(
            invoice,
            identity_result,
            None,
            None,
            (f"Current identity checks failed: {detail}",),
        )

    review = review_reader.read_review(invoice.pdf_po_number)
    state = repository.get_invoice_readonly(invoice.internet_message_id)
    blockers = []
    if review.po_number != invoice.pdf_po_number:
        blockers.append(
            f"Current FieldPO PO is {review.po_number or '<missing>'}; "
            f"invoice PO is {invoice.pdf_po_number}"
        )
    if review.vin != invoice.vin:
        blockers.append(
            f"Current FieldPO VIN is {review.vin or '<missing>'}; "
            f"invoice VIN is {invoice.vin}"
        )
    if review.mva != identity.mva:
        blockers.append(
            f"Current FieldPO MVA is {review.mva or '<missing>'}; "
            f"verified Sheet MVA is {identity.mva}"
        )
    if review.authorized_amount != invoice.invoice_amount:
        blockers.append(
            f"Current FieldPO amount is {review.authorized_amount}; "
            f"invoice amount is {invoice.invoice_amount}"
        )
    if review.po_status != "APPROVED":
        blockers.append(
            f"Current FieldPO PO status is {review.po_status}; approval is not confirmed"
        )
    if review.work_order_created_by.strip().casefold() != "steele, dirk":
        blockers.append(
            "Work Order Created By is not Steele, Dirk: "
            f"{review.work_order_created_by or '<missing>'}"
        )
    if state is None:
        blockers.append("No invoice ledger state is available to verify completed actions")
    else:
        if state.identity_status is not StageStatus.COMPLETE:
            blockers.append(
                f"Recorded identity status is {state.identity_status}"
            )
        if state.po_status is not StageStatus.COMPLETE:
            blockers.append(f"Recorded PO approval status is {state.po_status}")
        if state.work_order_status is not StageStatus.COMPLETE:
            blockers.append(
                f"Recorded work-order closure status is {state.work_order_status}"
            )
    return InvoiceReviewAudit(
        invoice,
        identity_result,
        review,
        state,
        tuple(blockers),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of invoices in the Needs Review folder."
    )
    parser.parse_args(argv)
    _run_audit()
    return 0


def _run_audit() -> None:
    config = _load_runtime_config()
    account_name = _required_text(config, "account_name")
    with TemporaryDirectory(prefix="invoice-needs-review-audit-") as attachment_dir:
        pythoncom.CoInitialize()
        try:
            namespace = win32com.client.Dispatch(
                "Outlook.Application"
            ).GetNamespace("MAPI")
            outlook_source = OutlookInvoiceSource(
                namespace,
                account_name=account_name,
                attachment_directory=Path(attachment_dir),
            )
            scan = outlook_source.scan_needs_review_for_audit()
        finally:
            pythoncom.CoUninitialize()

    _print_capture_failures(scan)
    if not scan.invoices:
        if scan.failures:
            print("No parsed invoices were available for audit.")
        else:
            print("No invoices are currently in Needs Review.")
        return

    spreadsheet_id = _required_text(config, "spreadsheet_id")
    sheet_name = _required_text(config, "sheet_name")
    service_account_json = _resolve_path(
        _required_text(config, "service_account_json")
    )
    sheet_reader = GoogleSheetIdentityReader(
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        service_account_json=service_account_json,
    )
    repository = InvoiceRepository(ROOT / "data" / "completed_invoices.db")
    audits = []
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
        identity_reader = FieldPOBrowserIdentityReader(page)
        resolver = StrictIdentityResolver(sheet_reader, identity_reader)
        review_reader = FieldPOBrowserReviewReader(page, identity_reader)
        for invoice in scan.invoices:
            audits.append(
                audit_invoice(invoice, resolver, review_reader, repository)
            )

    _print_audit_results(audits)


def _print_capture_failures(scan: OutlookScanResult) -> None:
    if not scan.failures:
        return
    print(f"Unparsed Needs Review messages: {len(scan.failures)}")
    for failure in scan.failures:
        print(f"  Subject: {failure.subject or '(not recorded)'}")
        print(f"  Capture failure: {failure.detail}")
        print("  Result: Not cleared; invoice data could not be fully checked.")


def _print_audit_results(audits: Sequence[InvoiceReviewAudit]) -> None:
    candidates = [audit for audit in audits if audit.candidate]
    print("Needs Review audit (read-only)")
    print(f"Parsed invoices audited: {len(audits)}")
    print(f"Fully verified candidates for your manual Processed move: {len(candidates)}")
    for audit in candidates:
        _print_audit(audit)
    remaining = [audit for audit in audits if not audit.candidate]
    print(f"Not cleared to move: {len(remaining)}")
    for audit in remaining:
        _print_audit(audit)
    print("No Outlook messages or workflow records were moved or modified.")


def _print_audit(audit: InvoiceReviewAudit) -> None:
    invoice = audit.invoice
    identity = audit.identity_result.identity
    mva = identity.mva if identity is not None else "(not verified)"
    if audit.candidate:
        label = "CANDIDATE"
    elif identity is not None and audit.identity_result.status is StageStatus.COMPLETE:
        label = "IDENTITY PASSES; OTHER GATES NOT CLEARED"
    else:
        label = "REMAINS IN NEEDS REVIEW"
    print(
        f"  {label}: Invoice {invoice.invoice_number}; PO {invoice.pdf_po_number}; "
        f"VIN {invoice.vin}; MVA {mva}; amount {invoice.invoice_amount}"
    )
    print(
        "    Current identity checks: "
        + (
            "PASS"
            if identity is not None
            and audit.identity_result.status is StageStatus.COMPLETE
            else "BLOCKED"
        )
    )
    if audit.fieldpo_review is not None:
        print(
            f"    FieldPO status: {audit.fieldpo_review.po_status}; "
            f"PO {audit.fieldpo_review.po_number}; "
            f"VIN {audit.fieldpo_review.vin}; MVA {audit.fieldpo_review.mva}; "
            f"Work Order Created By: "
            f"{audit.fieldpo_review.work_order_created_by or '<missing>'}"
        )
    if audit.ledger_state is not None:
        print(
            f"    Recorded approval/closure: PO {audit.ledger_state.po_status}; "
            f"work order {audit.ledger_state.work_order_status}"
        )
    for blocker in audit.blockers:
        print(f"    Not cleared: {blocker}")


if __name__ == "__main__":
    raise SystemExit(main())
