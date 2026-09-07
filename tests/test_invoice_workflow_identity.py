from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from invoice_workflow.contracts import (
    FieldPOIdentitySnapshot,
    ParsedInvoice,
    StageStatus,
)
from invoice_workflow.identity import StrictIdentityResolver


def _invoice() -> ParsedInvoice:
    return ParsedInvoice(
        internet_message_id="<message@example>",
        entry_id="entry",
        invoice_number="5294412",
        subject="[External] Invoice #5294412 (PO # FPO1131464)",
        received_at=datetime(2026, 9, 3, tzinfo=UTC),
        subject_po_number="FPO1131464",
        pdf_po_number="FPO1131464",
        vin="1C4SJSBP6SS537975",
        invoice_amount=Decimal("340.00"),
        pdf_path="invoice.pdf",
    )


def _resolver(sheet_mvas=("061093642",), **fieldpo_overrides):
    invoice = _invoice()
    snapshot = FieldPOIdentitySnapshot(
        po_number=fieldpo_overrides.get("po_number", invoice.subject_po_number),
        vin=fieldpo_overrides.get("vin", invoice.vin),
        mva=fieldpo_overrides.get("mva", sheet_mvas[0] if sheet_mvas else "061093642"),
        authorized_amount=fieldpo_overrides.get("authorized_amount", invoice.invoice_amount),
    )
    sheet = MagicMock()
    sheet.find_mvas_by_vin.return_value = sheet_mvas
    fieldpo = MagicMock()
    fieldpo.read_identity.return_value = snapshot
    return StrictIdentityResolver(sheet, fieldpo), sheet, fieldpo


def test_exact_identity_is_valid():
    resolver, sheet, fieldpo = _resolver()

    result = resolver.resolve(_invoice())

    assert result.status is StageStatus.COMPLETE
    assert result.identity.mva == "061093642"
    sheet.find_mvas_by_vin.assert_called_once_with("1C4SJSBP6SS537975")
    fieldpo.read_identity.assert_called_once_with("FPO1131464")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"subject_po_number": ""}, "subject does not contain"),
        ({"subject_po_number": "FPO112856"}, "subject does not contain"),
        ({"subject_po_number": "FPO11285A3"}, "subject does not contain"),
        ({"pdf_po_number": ""}, "PDF does not contain"),
        ({"pdf_po_number": "FPO112856"}, "PDF does not contain"),
        ({"pdf_po_number": "FPO11285A3"}, "PDF does not contain"),
        ({"pdf_po_number": "FPO9999999"}, "Subject/PDF PO mismatch"),
        ({"vin": "INVALID"}, "Invoice VIN is invalid"),
    ],
)
def test_local_identity_failure_blocks_before_external_reads(changes, expected):
    resolver, sheet, fieldpo = _resolver()

    result = resolver.resolve(replace(_invoice(), **changes))

    assert result.status is StageStatus.BLOCKED
    assert expected in result.detail
    sheet.find_mvas_by_vin.assert_not_called()
    fieldpo.read_identity.assert_not_called()


def test_multiple_sheet_mvas_block():
    resolver, _, _ = _resolver(sheet_mvas=("061093642", "061093643"))

    result = resolver.resolve(_invoice())

    assert result.status is StageStatus.BLOCKED
    assert "found 2" in result.detail


def test_repeated_sheet_rows_with_same_mva_resolve_once():
    resolver, _, _ = _resolver(
        sheet_mvas=("061093642", "061093642", "061093642")
    )

    result = resolver.resolve(_invoice())

    assert result.status is StageStatus.COMPLETE
    assert result.identity.mva == "061093642"


@pytest.mark.parametrize(
    ("fieldpo_overrides", "expected"),
    [
        ({"po_number": "FPO9999999"}, "FieldPO PO mismatch"),
        ({"vin": "1FMDE7BH9TLA47847"}, "FieldPO VIN mismatch"),
        ({"mva": "061093643"}, "FieldPO MVA mismatch"),
        ({"authorized_amount": Decimal("341.00")}, "Authorized amount mismatch"),
    ],
)
def test_fieldpo_mismatch_blocks(fieldpo_overrides, expected):
    resolver, _, _ = _resolver(**fieldpo_overrides)

    result = resolver.resolve(_invoice())

    assert result.status is StageStatus.BLOCKED
    assert expected in result.detail
