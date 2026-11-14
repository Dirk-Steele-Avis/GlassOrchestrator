from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from invoice_workflow.contracts import (
    FieldPOIdentitySnapshot,
    FieldPOReviewSnapshot,
    IdentityResult,
    ParsedInvoice,
    StageStatus,
    ValidatedIdentity,
)
from invoice_workflow.review_audit import audit_invoice
from invoice_workflow.store import InvoiceState


def _setup_audit():
    invoice = ParsedInvoice(
        internet_message_id="audit-message",
        entry_id="audit-entry",
        invoice_number="5523420",
        subject="[External] Invoice #5523420 (PO # FPO1190782)",
        received_at=datetime(2026, 11, 12, tzinfo=UTC),
        subject_po_number="FPO1190782",
        pdf_po_number="FPO1190782",
        vin="7SAYGDEE9PA041152",
        invoice_amount=Decimal("340.00"),
        pdf_path="invoice.pdf",
        invoice_total=Decimal("340.00"),
    )
    identity = ValidatedIdentity(
        internet_message_id=invoice.internet_message_id,
        invoice_number=invoice.invoice_number,
        po_number=invoice.pdf_po_number,
        vin=invoice.vin,
        mva="060062262",
        invoice_amount=invoice.invoice_amount,
        authorized_amount=invoice.invoice_amount,
    )
    resolver = MagicMock()
    resolver.resolve.return_value = IdentityResult(
        StageStatus.COMPLETE,
        identity=identity,
        fieldpo_snapshot=FieldPOIdentitySnapshot(
            po_number=invoice.pdf_po_number,
            vin=invoice.vin,
            mva=identity.mva,
            authorized_amount=invoice.invoice_amount,
        ),
        sheet_mvas=(identity.mva,),
    )
    review_reader = MagicMock()
    review_reader.read_review.return_value = FieldPOReviewSnapshot(
        po_status="APPROVED",
        authorized_amount=invoice.invoice_amount,
        work_order_created_by="Steele, Dirk",
        po_number=invoice.pdf_po_number,
        vin=invoice.vin,
        mva=identity.mva,
    )
    ledger_state = InvoiceState(
        internet_message_id=invoice.internet_message_id,
        invoice_number=invoice.invoice_number,
        subject_po_number=invoice.subject_po_number,
        pdf_po_number=invoice.pdf_po_number,
        vin=invoice.vin,
        mva=identity.mva,
        invoice_amount=str(invoice.invoice_amount),
        authorized_amount=str(invoice.invoice_amount),
        identity_status=StageStatus.COMPLETE,
        compass_status=StageStatus.COMPLETE,
        po_status=StageStatus.COMPLETE,
        work_order_status=StageStatus.COMPLETE,
        outlook_status=StageStatus.FAILED,
        last_error="Outlook finalization failed",
    )
    repository = MagicMock()
    repository.get_invoice_readonly.return_value = ledger_state
    return invoice, resolver, review_reader, repository


def test_audit_clears_only_invoice_with_all_checks_and_actions_complete():
    invoice, resolver, reader, repository = _setup_audit()

    audit = audit_invoice(invoice, resolver, reader, repository)

    assert audit.candidate
    assert audit.blockers == ()
    reader.read_review.assert_called_once_with(invoice.pdf_po_number)
    repository.get_invoice_readonly.assert_called_once_with(
        invoice.internet_message_id
    )


def test_audit_keeps_identity_mismatch_in_needs_review():
    invoice, resolver, reader, repository = _setup_audit()
    resolver.resolve.return_value = IdentityResult(
        StageStatus.BLOCKED,
        detail="FieldPO MVA mismatch: expected 060062262, found 060062263",
        sheet_mvas=("060062262",),
        fieldpo_snapshot=FieldPOIdentitySnapshot(
            po_number=invoice.pdf_po_number,
            vin=invoice.vin,
            mva="060062263",
            authorized_amount=invoice.invoice_amount,
        ),
    )

    audit = audit_invoice(invoice, resolver, reader, repository)

    assert not audit.candidate
    assert audit.blockers == (
        "Current identity checks failed: FieldPO MVA mismatch: "
        "expected 060062262, found 060062263",
    )
    reader.read_review.assert_not_called()
    repository.get_invoice_readonly.assert_not_called()


def test_audit_checks_live_po_identity_status_amount_and_creator():
    invoice, resolver, reader, repository = _setup_audit()
    reader.read_review.return_value = FieldPOReviewSnapshot(
        po_status="IN PROGRESS",
        authorized_amount=Decimal("341.00"),
        work_order_created_by="Another User",
        po_number="FPO9999999",
        vin="1FMDE7BH9TLA47847",
        mva="060062263",
    )

    audit = audit_invoice(invoice, resolver, reader, repository)

    assert not audit.candidate
    assert any("Current FieldPO PO is FPO9999999" in item for item in audit.blockers)
    assert any("Current FieldPO VIN is" in item for item in audit.blockers)
    assert any("Current FieldPO MVA is" in item for item in audit.blockers)
    assert any("Current FieldPO amount is 341.00" in item for item in audit.blockers)
    assert any("approval is not confirmed" in item for item in audit.blockers)
    assert any("is not Steele, Dirk" in item for item in audit.blockers)


@pytest.mark.parametrize(
    ("state_field", "state_value", "expected"),
    [
        ("po_status", StageStatus.PENDING, "Recorded PO approval status is pending"),
        (
            "work_order_status",
            StageStatus.FAILED,
            "Recorded work-order closure status is failed",
        ),
    ],
)
def test_audit_requires_recorded_approval_and_work_order_closure(
    state_field,
    state_value,
    expected,
):
    invoice, resolver, reader, repository = _setup_audit()
    state = repository.get_invoice_readonly.return_value
    repository.get_invoice_readonly.return_value = replace(
        state,
        **{state_field: state_value},
    )

    audit = audit_invoice(invoice, resolver, reader, repository)

    assert not audit.candidate
    assert expected in audit.blockers
