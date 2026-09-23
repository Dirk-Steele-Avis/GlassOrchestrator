"""Preview or close FieldPO purchase orders from one combined invoice PDF."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pdfplumber
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outlook import agn_invoices
from outlook.FieldPO_Closer.FieldPO_Closer import process_fieldpo_invoices

DEFAULT_PDF = Path(__file__).resolve().parent / "Avis Georgia Invoices.pdf"
INVOICE_PATTERN = re.compile(r"(?im)^\s*Invoice\s+#\s*(\d+)\s*$")
PO_PATTERN = re.compile(r"\bPO\s*#\s*(FPO\d{7,})\b", re.IGNORECASE)
VIN_PATTERN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
SUBTOTAL_PATTERN = re.compile(
    r"(?im)^\s*Subtotal\s*\$?([\d,]+\.\d{2})\s*$"
)


@dataclass(frozen=True, slots=True)
class CombinedPdfInvoice:
    page_number: int
    invoice_number: str
    vin: str
    subject_po_number: str
    pdf_po_number: str
    invoice_amount: Decimal
    internet_message_id: str


class LocalPdfSource:
    """Satisfy the closer interface without changing Outlook."""

    def move_to_processed(self, internet_message_id: str) -> None:
        return None


def parse_combined_pdf(path: str | Path) -> list[CombinedPdfInvoice]:
    pdf_path = Path(path)
    invoices: list[CombinedPdfInvoice] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            invoice_numbers = INVOICE_PATTERN.findall(text)
            po_numbers = [value.upper() for value in PO_PATTERN.findall(text)]
            vins = [value.upper() for value in VIN_PATTERN.findall(text)]
            subtotals = SUBTOTAL_PATTERN.findall(text)
            counts = tuple(map(len, (invoice_numbers, po_numbers, vins, subtotals)))

            if counts == (0, 0, 0, 0):
                continue
            if counts != (1, 1, 1, 1):
                raise ValueError(
                    f"Page {page_number} must contain exactly one invoice number, "
                    f"PO, VIN, and subtotal; found {counts}"
                )

            try:
                amount = Decimal(subtotals[0].replace(",", ""))
            except InvalidOperation as exc:
                raise ValueError(
                    f"Page {page_number} has an invalid subtotal: {subtotals[0]}"
                ) from exc

            invoice_number = invoice_numbers[0]
            po_number = po_numbers[0]
            invoices.append(
                CombinedPdfInvoice(
                    page_number=page_number,
                    invoice_number=invoice_number,
                    vin=vins[0],
                    subject_po_number=po_number,
                    pdf_po_number=po_number,
                    invoice_amount=amount,
                    internet_message_id=f"local-pdf:{pdf_path.name}:{invoice_number}",
                )
            )

    if not invoices:
        raise ValueError(f"No invoice pages found in {pdf_path}")
    _reject_duplicates(invoices, "invoice number", lambda invoice: invoice.invoice_number)
    _reject_duplicates(invoices, "PO number", lambda invoice: invoice.subject_po_number)
    return invoices


def _reject_duplicates(invoices, label, key) -> None:
    counts = Counter(key(invoice) for invoice in invoices)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate {label}s in PDF: {', '.join(duplicates)}")


def group_by_vin(invoices: list[CombinedPdfInvoice]) -> list[list[CombinedPdfInvoice]]:
    groups: dict[str, list[CombinedPdfInvoice]] = {}
    for invoice in invoices:
        groups.setdefault(invoice.vin, []).append(invoice)
    return list(groups.values())


def print_preview(path: Path, groups: list[list[CombinedPdfInvoice]]) -> None:
    invoice_count = sum(len(group) for group in groups)
    print(f"PDF: {path}")
    print(f"Parsed {invoice_count} invoice(s) for {len(groups)} VIN(s).")
    for group in groups:
        details = ", ".join(
            f"{invoice.subject_po_number} ${invoice.invoice_amount:.2f}"
            for invoice in group
        )
        print(f"  {group[0].vin}: {details}")


def submit(groups: list[list[CombinedPdfInvoice]]) -> int:
    source = LocalPdfSource()
    failures = 0
    with sync_playwright() as playwright:
        context, page = agn_invoices.connect_to_fieldpo(playwright)
        try:
            for position, invoices in enumerate(groups, start=1):
                vin = invoices[0].vin
                try:
                    result = process_fieldpo_invoices(page, invoices, source)
                except Exception as exc:
                    result = "Review"
                    print(f"[{position}/{len(groups)}] {vin}: Review ({type(exc).__name__}: {exc})")
                else:
                    print(f"[{position}/{len(groups)}] {vin}: {result}")
                if result != "Success":
                    failures += 1
        finally:
            context.close()
    print(f"Complete: {len(groups) - failures} succeeded; {failures} require review.")
    return 2 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close exact FieldPO POs found in one combined invoice PDF."
    )
    parser.add_argument("pdf", nargs="?", type=Path, default=DEFAULT_PDF)
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Approve matching POs and close each VIN's work order after its final PO.",
    )
    args = parser.parse_args()

    invoices = parse_combined_pdf(args.pdf)
    groups = group_by_vin(invoices)
    print_preview(args.pdf, groups)
    if not args.submit:
        print("Preview only. Re-run with --submit to process exact FieldPO matches.")
        return 0
    return submit(groups)


if __name__ == "__main__":
    raise SystemExit(main())