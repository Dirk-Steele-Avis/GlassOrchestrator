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


class InvoicePdfParseError(ValueError):
    """Raised when one required invoice value cannot be parsed exactly."""


def parse_invoice_pdf(path: str | Path) -> InvoicePdfData:
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
    if len(vin_matches) != 1:
        raise InvoicePdfParseError(f"Expected exactly one VIN, found {len(vin_matches)}")

    amount_matches = re.findall(r"(?im)^\s*Subtotal\s*\$?([\d,]+\.\d{2})\s*$", text)
    if len(amount_matches) != 1:
        raise InvoicePdfParseError(
            f"Expected exactly one invoice subtotal, found {len(amount_matches)}"
        )
    try:
        amount = Decimal(amount_matches[0].replace(",", ""))
    except InvalidOperation as exc:
        raise InvoicePdfParseError("Invoice subtotal is not a valid amount") from exc

    return InvoicePdfData(
        po_number=next(iter(po_matches)),
        vin=next(iter(vin_matches)),
        amount=amount,
    )
