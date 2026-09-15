"""Close exact FieldPO POs for VINs with one matching Outlook invoice."""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from invoice_workflow.outlook_source import OutlookInvoiceSource
from outlook import agn_invoices

TOOL_DIR = Path(__file__).resolve().parent
INPUT_CSV = TOOL_DIR / "vins.csv"
ATTACHMENT_DIR = TOOL_DIR / "attachments"
CONFIG_PATH = ROOT / "outlook" / "agn_invoices_config.json"
PROCESSABLE_RESULTS = {"", "missing invoice"}
VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
PO_PATTERN = re.compile(r"FPO\d{7,}", re.IGNORECASE)


@dataclass(slots=True)
class VinRow:
    vin: str
    result: str


def load_rows(path: Path = INPUT_CSV) -> list[VinRow]:
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames != ["VIN", "Result"]:
            raise ValueError("vins.csv must contain exactly these headers: VIN,Result")
        return [
            VinRow(
                str(row.get("VIN") or "").strip().upper(),
                str(row.get("Result") or "").strip(),
            )
            for row in reader
        ]


def save_rows(rows: list[VinRow], path: Path = INPUT_CSV) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["VIN", "Result"])
        writer.writerows((row.vin, row.result) for row in rows)
    temporary_path.replace(path)


def process_rows(
    rows: list[VinRow],
    invoices: list[Any],
    process_invoices: Callable[[list[Any]], str],
    save: Callable[[list[VinRow]], None],
) -> None:
    eligible_by_vin: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.result.strip().casefold() in PROCESSABLE_RESULTS:
            eligible_by_vin.setdefault(row.vin, []).append(index)
    invoices_by_vin: dict[str, list[Any]] = {}
    for invoice in invoices:
        invoices_by_vin.setdefault(str(invoice.vin).strip().upper(), []).append(invoice)

    for position, (vin, indices) in enumerate(eligible_by_vin.items(), start=1):
        detail = ""
        if VIN_PATTERN.fullmatch(vin) is None:
            result = "Review"
            detail = "invalid VIN"
        else:
            matches = invoices_by_vin.get(vin, [])
            if not matches:
                result = "Missing Invoice"
                detail = "no matching invoice in the Invoice folder"
            else:
                try:
                    result = process_invoices(matches)
                    detail = (
                        "FieldPO checks passed"
                        if result == "Success"
                        else "a FieldPO gate requires review"
                    )
                except Exception as exc:
                    result = "Review"
                    detail = f"{type(exc).__name__}: {exc}"
        for index in indices:
            rows[index].result = result
        save(rows)
        print(
            f"[{position}/{len(eligible_by_vin)}] {vin or '<blank VIN>'}: "
            f"{result} ({detail}; rows={len(indices)})",
            flush=True,
        )


def process_fieldpo_invoices(
    page: Any,
    invoices: list[Any],
    outlook_source: OutlookInvoiceSource,
) -> str:
    po_numbers = [str(invoice.subject_po_number).strip().upper() for invoice in invoices]
    if any(PO_PATTERN.fullmatch(po_number) is None for po_number in po_numbers):
        return "Review"
    if len(set(po_numbers)) != len(po_numbers):
        return "Review"

    expected_po_numbers = set(po_numbers)
    for position, invoice in enumerate(invoices, start=1):
        result = process_fieldpo_invoice(
            page,
            invoice,
            outlook_source,
            expected_po_numbers=expected_po_numbers,
            close_work_order=position == len(invoices),
        )
        if result != "Success":
            return "Review"
    return "Success"


def process_fieldpo_invoice(
    page: Any,
    invoice: Any,
    outlook_source: OutlookInvoiceSource,
    *,
    expected_po_numbers: set[str] | None = None,
    close_work_order: bool = True,
) -> str:
    subject_po = str(invoice.subject_po_number).strip().upper()
    pdf_po = str(invoice.pdf_po_number).strip().upper()
    if not subject_po or subject_po != pdf_po:
        return "Review"

    found_mva = agn_invoices.go_to_active_work_order_tab(page, invoice.vin)
    if not found_mva or not agn_invoices.po_exists(page, subject_po):
        return "Review"

    listed_pos = _listed_po_numbers(page)
    if listed_pos != (expected_po_numbers or {subject_po}):
        return "Review"

    created_by = agn_invoices.read_work_order_created_by(page)
    if created_by.strip().casefold() != "steele, dirk":
        return "Review"

    agn_invoices.click_into_po(page, subject_po)
    status, authorized_amount = agn_invoices.read_po_status_and_amount(page)
    if status == "APPROVED":
        return "Review"
    if status != "IN PROGRESS" or _amount(authorized_amount) != invoice.invoice_amount:
        return "Review"

    closed = agn_invoices.approve_displayed_po(
        page,
        invoice.vin,
        request_permission=False,
        expected_po_number=subject_po,
        close_work_order=close_work_order,
        allow_open_if_closure_unavailable=False,
    )
    if closed != close_work_order:
        return "Review"

    outlook_source.move_to_processed(invoice.internet_message_id)
    return "Success"


def _listed_po_numbers(page: Any) -> set[str]:
    texts = page.get_by_text(PO_PATTERN, exact=True).all_inner_texts()
    return {text.strip().upper() for text in texts if PO_PATTERN.fullmatch(text.strip())}


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def _load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        payload = json.load(config_file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {CONFIG_PATH}")
    return payload


def main() -> int:
    rows = load_rows()
    eligible_count = sum(
        row.result.strip().casefold() in PROCESSABLE_RESULTS for row in rows
    )
    print(
        f"FieldPO Closer: loaded {len(rows)} row(s); "
        f"{eligible_count} eligible for processing.",
        flush=True,
    )
    if eligible_count == 0:
        print("Nothing to process. Eligible results are blank or Missing Invoice.", flush=True)
        return 0

    config = _load_config()
    account_name = str(config.get("account_name", "")).strip()
    if not account_name:
        raise ValueError(f"Missing account_name in {CONFIG_PATH}")

    pythoncom.CoInitialize()
    try:
        print("Scanning the Outlook Invoice folder...", flush=True)
        namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        outlook_source = OutlookInvoiceSource(
            namespace,
            account_name=account_name,
            attachment_directory=ATTACHMENT_DIR,
        )
        scan = outlook_source.scan_parent_invoices()
        print(
            f"Outlook scan complete: {len(scan.invoices)} valid invoice(s), "
            f"{len(scan.failures)} invoice(s) requiring review.",
            flush=True,
        )
        playwright_manager = None
        context = None
        page = None

        def process_invoices(matching_invoices: list[Any]) -> str:
            nonlocal playwright_manager, context, page
            if page is None:
                playwright_manager = sync_playwright()
                playwright = playwright_manager.__enter__()
                context, page = agn_invoices.connect_to_fieldpo(playwright)
            return process_fieldpo_invoices(page, matching_invoices, outlook_source)

        try:
            process_rows(rows, list(scan.invoices), process_invoices, save_rows)
        finally:
            if context is not None:
                context.close()
            if playwright_manager is not None:
                playwright_manager.__exit__(None, None, None)
    finally:
        pythoncom.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())