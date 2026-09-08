"""Read-only adapters used by strict completed-invoice identity validation."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_workflow.contracts import FieldPOIdentitySnapshot
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
