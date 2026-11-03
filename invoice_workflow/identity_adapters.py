"""FieldPO identity readers and strict PO approval adapter."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_workflow.contracts import (
    FieldPOIdentitySnapshot,
    FieldPOReviewSnapshot,
    ParsedInvoice,
    ValidatedIdentity,
)
from vendor_tracking.sheet_updater import VendorSheetUpdater

FIELDPO_URL = "https://supply-chain.east.prod.sdp.abg.cloud/fieldpo/dashboard"


class GoogleSheetIdentityReader:
    """Read every exact MVA value associated with one VIN."""

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        sheet_name: str,
        service_account_json: str | Path,
    ) -> None:
        self._updater = VendorSheetUpdater(
            spreadsheet_id,
            sheet_name,
            str(service_account_json),
        )
        self._updater.connect()

    def find_mvas_by_vin(self, vin: str) -> tuple[str, ...]:
        values = tuple(
            self._updater.get_row_fields(row, ["MVA"]).get("MVA", "").strip()
            for row in self._updater.find_rows_by_vin(vin)
        )
        return tuple(
            value.zfill(9) if re.fullmatch(r"\d{8,9}", value) else value
            for value in values
        )

    def find_vins_by_mva(self, mva: str) -> tuple[str, ...]:
        values = (
            self._updater.get_row_fields(row, ["VIN"]).get("VIN", "").strip().upper()
            for row in self._updater.find_rows_by_mva(mva)
        )
        return tuple(dict.fromkeys(value for value in values if value))


class FieldPOBrowserIdentityReader:
    """Read identity through one exact PO-search browser path."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def read_identity(self, po_number: str) -> FieldPOIdentitySnapshot:
        if re.fullmatch(r"FPO\d{7,}", po_number) is None:
            raise ValueError(f"Invalid exact PO number: {po_number}")

        page = self._page
        page.goto(FIELDPO_URL)
        page.wait_for_url("**/fieldpo/dashboard**", timeout=120_000)
        _wait_for_loading_overlay(page)
        _dismiss_attention_popup(page)
        page.get_by_text("Search", exact=True).click()
        search_input = page.get_by_placeholder("WO#, PO#, MVA, VIN", exact=True)
        search_input.wait_for(state="visible", timeout=30_000)
        if search_input.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO search input, found {search_input.count()}"
            )
        search_input.fill(po_number)
        search_button = page.get_by_role("button", name="Search", exact=True)
        if search_button.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO Search button, found {search_button.count()}"
            )
        search_button.click()

        search_result = page.locator("text=MVA#")
        search_result.first.wait_for(state="visible", timeout=30_000)
        if search_result.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO search result for {po_number}, "
                f"found {search_result.count()}"
            )
        result_text = search_result.locator("xpath=..").inner_text()
        displayed_mva = _extract_exact(
            result_text,
            r"\bMVA\s*#?\s*:?\s*(\d{8,9})\b",
            "MVA",
        )
        mva = displayed_mva.zfill(9)

        result_card = search_result.locator(
            "xpath=ancestor::*[contains(@class, 'card')][1]"
        )
        card_text = result_card.inner_text()
        work_order_number = _extract_exact(
            card_text,
            r"\bWO\s*#\s*:?\s*([A-Z0-9-]+)\b",
            "work order number",
        )
        search_result.click()
        work_order_link = page.locator(
            "span.boldText.cursorPointer.primaryColor"
        ).filter(has_text=re.compile(rf"^\s*{re.escape(work_order_number)}\s*$"))
        work_order_link.wait_for(state="visible", timeout=30_000)
        if work_order_link.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO WO link {work_order_number}, "
                f"found {work_order_link.count()}"
            )
        work_order_link.click()

        vehicle_detail = page.get_by_text("Vehicle Details", exact=True)
        vehicle_detail.wait_for(state="visible", timeout=30_000)
        if vehicle_detail.count() != 1:
            raise RuntimeError(
                f"Expected exactly one Vehicle Details tab, found {vehicle_detail.count()}"
            )
        vehicle_detail.click()
        detail_text = page.locator("body").inner_text()
        vin = _extract_exact(
            detail_text,
            r"\bVIN\s*#?\s*:?\s*([A-HJ-NPR-Z0-9]{17})\b",
            "VIN",
        )

        active_work_order = page.get_by_text("Active Work Order", exact=True)
        if active_work_order.count() != 1:
            raise RuntimeError(
                "Expected exactly one Active Work Order tab, "
                f"found {active_work_order.count()}"
            )
        active_work_order.click()
        po_result = page.get_by_text(po_number, exact=True)
        po_result.wait_for(state="visible", timeout=30_000)
        if po_result.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO PO {po_number}, found {po_result.count()}"
            )
        po_card_text = po_result.locator(
            "xpath=ancestor::*[contains(@class, 'card')][1]"
        ).inner_text()
        amount_value = _extract_exact(
            po_card_text,
            r"\$\s*([\d,]+\.\d{2})\b",
            "authorized amount",
        )
        try:
            authorized_amount = Decimal(amount_value.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(
                f"FieldPO authorized amount is invalid: {amount_value}"
            ) from exc

        return FieldPOIdentitySnapshot(
            po_number=po_number,
            vin=vin,
            mva=mva,
            authorized_amount=authorized_amount,
        )


class FieldPOBrowserReviewReader:
    """Read current PO status and amount without mutating FieldPO."""

    def __init__(
        self,
        page: Any,
        identity_reader: FieldPOBrowserIdentityReader | None = None,
    ) -> None:
        self._page = page
        self._identity_reader = identity_reader or FieldPOBrowserIdentityReader(page)

    def read_review(self, po_number: str) -> FieldPOReviewSnapshot:
        identity = self._identity_reader.read_identity(po_number)
        po_result = self._page.get_by_text(po_number, exact=True)
        if po_result.count() != 1:
            raise RuntimeError(
                f"Expected exactly one FieldPO PO {po_number}, found {po_result.count()}"
            )
        po_card_text = po_result.locator(
            "xpath=ancestor::*[contains(@class, 'card')][1]"
        ).inner_text()
        statuses = tuple(
            dict.fromkeys(
                status.upper()
                for status in re.findall(
                    r"(?im)^\s*(APPROVED|IN PROGRESS)\s*$",
                    po_card_text,
                )
            )
        )
        if len(statuses) != 1:
            raise ValueError(
                f"Expected exactly one recognized FieldPO PO status, found {len(statuses)}"
            )
        return FieldPOReviewSnapshot(
            po_status=statuses[0],
            authorized_amount=identity.authorized_amount,
        )


class FieldPOBrowserApprover:
    """Approve one already-validated PO and close its exact FieldPO work order."""

    def __init__(
        self,
        page: Any,
        identity_reader: FieldPOBrowserIdentityReader | None = None,
    ) -> None:
        self._page = page
        self._identity_reader = identity_reader or FieldPOBrowserIdentityReader(page)

    def approve(self, invoice: ParsedInvoice, identity: ValidatedIdentity) -> None:
        from outlook import agn_invoices

        po_number = invoice.pdf_po_number.strip().upper()
        if not po_number or po_number != identity.po_number:
            raise ValueError(
                f"PDF PO {po_number or '<missing>'} does not match validated "
                f"FieldPO PO {identity.po_number}"
            )
        subject_po = invoice.subject_po_number.strip().upper()
        if subject_po and subject_po != po_number:
            raise ValueError(
                f"Email subject PO {subject_po} does not match PDF PO {po_number}"
            )
        if identity.vin != invoice.vin:
            raise ValueError(
                f"Validated VIN {identity.vin} does not match invoice VIN {invoice.vin}"
            )
        if identity.invoice_amount != invoice.invoice_amount:
            raise ValueError(
                f"Validated amount {identity.invoice_amount} does not match "
                f"invoice amount {invoice.invoice_amount}"
            )

        page = self._page
        live_identity = self._identity_reader.read_identity(po_number)
        if live_identity.po_number != identity.po_number:
            raise ValueError(
                f"FieldPO PO {live_identity.po_number} changed from validated PO "
                f"{identity.po_number}"
            )
        if live_identity.vin != identity.vin or live_identity.vin != invoice.vin:
            raise ValueError(
                f"Live FieldPO VIN {live_identity.vin} does not match invoice/"
                f"validated VIN {invoice.vin}"
            )
        if live_identity.mva != identity.mva:
            raise ValueError(
                f"Live FieldPO MVA {live_identity.mva or '<missing>'} does not match "
                f"Sheet MVA {identity.mva}"
            )
        if live_identity.authorized_amount != invoice.invoice_amount:
            raise ValueError(
                f"Live FieldPO authorized amount {live_identity.authorized_amount} "
                f"does not match invoice amount {invoice.invoice_amount}"
            )
        if live_identity.authorized_amount != identity.authorized_amount:
            raise ValueError(
                f"Live FieldPO authorized amount {live_identity.authorized_amount} "
                f"changed from validated amount {identity.authorized_amount}"
            )
        work_order_created_by = agn_invoices.read_work_order_created_by(page)
        if work_order_created_by.strip().casefold() != "steele, dirk":
            raise ValueError(
                "Work Order Created By must be Steele, Dirk; found "
                f"{work_order_created_by or '<missing>'}"
            )
        if not agn_invoices.po_exists(page, po_number):
            raise LookupError(f"Exact FieldPO PO {po_number} is not present")

        agn_invoices.click_into_po(page, po_number)
        status, authorized_amount = agn_invoices.read_po_status_and_amount(page)
        if status != "IN PROGRESS":
            raise ValueError(
                f"FieldPO PO {po_number} status is {status}; expected IN PROGRESS"
            )
        amount = _parse_authorized_amount(authorized_amount)
        if amount != invoice.invoice_amount:
            raise ValueError(
                f"FieldPO authorized amount {amount} does not match invoice amount "
                f"{invoice.invoice_amount}"
            )
        if amount != live_identity.authorized_amount:
            raise ValueError(f"FieldPO authorized amount changed during approval check")

        work_order_closed = agn_invoices.approve_displayed_po(
            page,
            identity.vin,
            request_permission=False,
            expected_po_number=po_number,
            close_work_order=True,
            allow_open_if_closure_unavailable=False,
        )
        if work_order_closed is not True:
            raise RuntimeError(
                f"FieldPO PO {po_number} approval and work-order closure "
                "were not both confirmed"
            )


def _parse_authorized_amount(value: str) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid FieldPO authorized amount: {value!r}") from exc


def _wait_for_loading_overlay(page: Any) -> None:
    overlay = page.locator(".ngx-spinner-overlay")
    overlay.wait_for(
        state="visible",
        timeout=30_000,
    )
    overlay.wait_for(
        state="hidden",
        timeout=120_000,
    )


def _dismiss_attention_popup(page: Any) -> None:
    popup = page.locator(".popup")
    if popup.count() == 0 or not popup.is_visible():
        return
    if popup.count() != 1:
        raise RuntimeError(
            f"Expected exactly one FieldPO attention popup, found {popup.count()}"
        )
    resume_button = popup.get_by_role("button", name="Resume Work", exact=True)
    if resume_button.count() != 1:
        raise RuntimeError(
            "Expected exactly one Resume Work button in FieldPO attention popup, "
            f"found {resume_button.count()}"
        )
    resume_button.click()
    popup.wait_for(state="hidden", timeout=30_000)


def _extract_exact(text: str, pattern: str, label: str) -> str:
    matches = re.findall(pattern, text, re.IGNORECASE)
    unique = tuple(dict.fromkeys(match.upper() for match in matches))
    if len(unique) != 1:
        raise ValueError(f"Expected exactly one FieldPO {label}, found {len(unique)}")
    return unique[0]
