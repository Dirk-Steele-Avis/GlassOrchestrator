"""Strict one-pass parser for AGN invoice PDFs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber


@dataclass(frozen=True, slots=True)
class InvoicePdfData:
    po_number: str
    vin: str
    amount: Decimal
    is_recalibration: bool = False
    customer_address: str = ""
    total_amount: Decimal | None = None


class InvoicePdfParseError(ValueError):
    """Raised when one required invoice value cannot be parsed exactly."""


def parse_invoice_pdf(
    path: str | Path,
    *,
    allow_missing_vin: bool = False,
    require_total: bool = False,
) -> InvoicePdfData:
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not re.search(r"(?im)^\s*Invoice\s+#\d+\s*$", text):
        raise InvoicePdfParseError("PDF does not contain an exact invoice heading")

    po_matches = {
        match.upper()
        for match in re.findall(r"\bPO\s*#\s*(FPO\d{7,})\b", text, re.IGNORECASE)
    }
    if len(po_matches) != 1:
        raise InvoicePdfParseError(
            f"Expected exactly one PDF PO number, found {len(po_matches)}"
        )

    vin_matches = set(re.findall(r"\b([A-HJ-NPR-Z0-9]{17})\b", text.upper()))
    if len(vin_matches) > 1 or (not vin_matches and not allow_missing_vin):
        expectation = "at most one" if allow_missing_vin else "exactly one"
        raise InvoicePdfParseError(
            f"Expected {expectation} VIN, found {len(vin_matches)}"
        )

    amount_matches = re.findall(r"(?im)^\s*Subtotal\s*\$?([\d,]+\.\d{2})\s*$", text)
    if len(amount_matches) != 1:
        raise InvoicePdfParseError(
            f"Expected exactly one invoice subtotal, found {len(amount_matches)}"
        )
    try:
        amount = Decimal(amount_matches[0].replace(",", ""))
    except InvalidOperation as exc:
        raise InvoicePdfParseError("Invoice subtotal is not a valid amount") from exc

    total_matches = re.findall(r"(?im)^\s*Total\s*\$?([\d,]+\.\d{2})\s*$", text)
    if len(total_matches) > 1 or (require_total and len(total_matches) != 1):
        raise InvoicePdfParseError(
            f"Expected exactly one invoice total, found {len(total_matches)}"
        )
    total_amount = None
    if total_matches:
        try:
            total_amount = Decimal(total_matches[0].replace(",", ""))
        except InvalidOperation as exc:
            raise InvoicePdfParseError("Invoice total is not a valid amount") from exc

    return InvoicePdfData(
        po_number=next(iter(po_matches)),
        vin=next(iter(vin_matches), ""),
        amount=amount,
        is_recalibration=bool(re.search(
            r"(?im)^\s*\d+\.\d+\s+ea\b[^\n]*\b(?:recalibration|calibration)\b",
            text,
        )),
        customer_address=_customer_address(text),
        total_amount=total_amount,
    )


def _customer_address(text: str) -> str:
    sections = re.findall(
        r"Customer Information[^\n]*\n(.*?)^\s*Telephone\b",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if len(sections) != 1:
        return ""
    addresses = re.findall(
        r"(?im)^\s*Address[ \t]+([^\n]*)$",
        sections[0],
    )
    if len(addresses) != 1:
        return ""
    return re.split(r"\bModel\b", addresses[0], maxsplit=1, flags=re.IGNORECASE)[0].strip()


def validate_vendor_invoice(invoice: InvoicePdfData) -> None:
    """Enforce vendor recalibration rules before any workflow mutation."""
    if not invoice.is_recalibration:
        return
    if not invoice.customer_address:
        raise InvoicePdfParseError(
            "Recalibration customer address is missing or ambiguous"
        )
    address = " ".join(invoice.customer_address.upper().split())
    in_house_addresses = {
        "3333 BOBBY BROWN PARKWAY": "BB",
        "4340 GLOBAL GATEWAY CONNECTOR": "APO",
    }
    if address in in_house_addresses:
        raise InvoicePdfParseError(
            f"Vendor recalibration is not permitted for {in_house_addresses[address]} "
            f"({invoice.customer_address}); recalibration is performed in-house"
        )
    if invoice.amount != Decimal("150.00"):
        raise InvoicePdfParseError(
            f"Vendor recalibration must be exactly $150.00; found ${invoice.amount}"
        )
