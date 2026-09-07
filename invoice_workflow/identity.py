"""Strict PO, VIN, MVA, and amount identity validation."""

from __future__ import annotations

import re

from invoice_workflow.contracts import (
    FieldPOIdentityReader,
    IdentityResult,
    ParsedInvoice,
    SheetIdentityReader,
    StageStatus,
    ValidatedIdentity,
)

_PO_PATTERN = re.compile(r"FPO\d{7,}")
_VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_MVA_PATTERN = re.compile(r"\d{9}")


class StrictIdentityResolver:
    """Resolve one invoice through one exact cross-system identity path."""

    def __init__(
        self,
        sheet_reader: SheetIdentityReader,
        fieldpo_reader: FieldPOIdentityReader,
    ) -> None:
        self._sheet_reader = sheet_reader
        self._fieldpo_reader = fieldpo_reader

    def resolve(self, invoice: ParsedInvoice) -> IdentityResult:
        local_error = _validate_local_invoice(invoice)
        if local_error:
            return IdentityResult(StageStatus.BLOCKED, detail=local_error)

        fieldpo = self._fieldpo_reader.read_identity(invoice.subject_po_number)
        sheet_mvas = tuple(self._sheet_reader.find_mvas_by_vin(invoice.vin))
        invalid_sheet_mvas = tuple(
            mva for mva in sheet_mvas if _MVA_PATTERN.fullmatch(mva) is None
        )
        if invalid_sheet_mvas:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=f"Sheet contains invalid MVA values for VIN {invoice.vin}",
            )
        distinct_sheet_mvas = tuple(dict.fromkeys(sheet_mvas))
        if len(distinct_sheet_mvas) != 1:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=(
                    f"Expected exactly one distinct Sheet MVA for VIN {invoice.vin}, "
                    f"found {len(distinct_sheet_mvas)}"
                ),
            )
        sheet_mva = distinct_sheet_mvas[0]
        if fieldpo.po_number != invoice.subject_po_number:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=(
                    f"FieldPO PO mismatch: expected {invoice.subject_po_number}, "
                    f"found {fieldpo.po_number}"
                ),
            )
        if fieldpo.vin != invoice.vin:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=(
                    f"FieldPO VIN mismatch: expected {invoice.vin}, "
                    f"found {fieldpo.vin}"
                ),
            )
        if fieldpo.mva != sheet_mva:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=(
                    f"FieldPO MVA mismatch: expected {sheet_mva}, "
                    f"found {fieldpo.mva}"
                ),
            )
        if fieldpo.authorized_amount != invoice.invoice_amount:
            return IdentityResult(
                StageStatus.BLOCKED,
                detail=(
                    f"Authorized amount mismatch: invoice {invoice.invoice_amount}, "
                    f"FieldPO {fieldpo.authorized_amount}"
                ),
            )

        return IdentityResult(
            StageStatus.COMPLETE,
            identity=ValidatedIdentity(
                internet_message_id=invoice.internet_message_id,
                invoice_number=invoice.invoice_number,
                po_number=invoice.subject_po_number,
                vin=invoice.vin,
                mva=sheet_mva,
                invoice_amount=invoice.invoice_amount,
                authorized_amount=fieldpo.authorized_amount,
            ),
        )


def _validate_local_invoice(invoice: ParsedInvoice) -> str:
    if _PO_PATTERN.fullmatch(invoice.subject_po_number) is None:
        return "Invoice subject does not contain one exact PO number"
    if _PO_PATTERN.fullmatch(invoice.pdf_po_number) is None:
        return "Invoice PDF does not contain one exact PO number"
    if invoice.subject_po_number != invoice.pdf_po_number:
        return (
            f"Subject/PDF PO mismatch: {invoice.subject_po_number} != "
            f"{invoice.pdf_po_number}"
        )
    if _VIN_PATTERN.fullmatch(invoice.vin) is None:
        return f"Invoice VIN is invalid: {invoice.vin}"
    return ""
