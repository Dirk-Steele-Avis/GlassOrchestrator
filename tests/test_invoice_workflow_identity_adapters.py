from decimal import Decimal
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from invoice_workflow.contracts import (
    FieldPOIdentitySnapshot,
    ParsedInvoice,
    ValidatedIdentity,
)
from invoice_workflow.identity_adapters import (
    FieldPOBrowserApprover,
    FieldPOBrowserReviewReader,
)


def _reader(card_text: str):
    page = MagicMock()
    po_result = page.get_by_text.return_value
    po_result.count.return_value = 1
    po_result.locator.return_value.inner_text.return_value = card_text
    identity_reader = MagicMock()
    identity_reader.read_identity.return_value = FieldPOIdentitySnapshot(
        po_number="FPO1131464",
        vin="1C4SJSBP6SS537975",
        mva="057935872",
        authorized_amount=Decimal("340.00"),
    )
    return FieldPOBrowserReviewReader(page, identity_reader), page, identity_reader


def _approval_values(subject_po="FPO1131464"):
    invoice = ParsedInvoice(
        internet_message_id="<approval@example>",
        entry_id="approval-entry",
        invoice_number="5294412",
        subject="[External] Invoice #5294412",
        received_at=datetime(2026, 9, 3, tzinfo=UTC),
        subject_po_number=subject_po,
        pdf_po_number="FPO1131464",
        vin="1C4SJSBP6SS537975",
        invoice_amount=Decimal("340.00"),
        pdf_path="invoice.pdf",
    )
    identity = ValidatedIdentity(
        internet_message_id=invoice.internet_message_id,
        invoice_number=invoice.invoice_number,
        po_number="FPO1131464",
        vin=invoice.vin,
        mva="057935872",
        invoice_amount=invoice.invoice_amount,
        authorized_amount=invoice.invoice_amount,
    )
    return invoice, identity


def _approval_dependencies(
    monkeypatch,
    *,
    live_po="FPO1131464",
    live_vin="1C4SJSBP6SS537975",
    live_mva="057935872",
    live_amount=Decimal("340.00"),
    creator="Steele, Dirk",
    status="IN PROGRESS",
    amount="340.00",
):
    from outlook import agn_invoices

    identity_reader = MagicMock()
    identity_reader.read_identity.return_value = FieldPOIdentitySnapshot(
        po_number=live_po,
        vin=live_vin,
        mva=live_mva,
        authorized_amount=live_amount,
    )
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", MagicMock(return_value=creator))
    monkeypatch.setattr(agn_invoices, "po_exists", MagicMock(return_value=True))
    monkeypatch.setattr(agn_invoices, "click_into_po", MagicMock())
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        MagicMock(return_value=(status, amount)),
    )
    approve = MagicMock(return_value=True)
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve)
    return agn_invoices, approve, identity_reader


def test_fieldpo_approver_checks_exact_gates_and_closes_work_order(monkeypatch):
    invoice, identity = _approval_values(subject_po="")
    agn_invoices, approve, identity_reader = _approval_dependencies(monkeypatch)
    page = MagicMock()

    FieldPOBrowserApprover(page, identity_reader).approve(invoice, identity)

    identity_reader.read_identity.assert_called_once_with(identity.po_number)
    agn_invoices.po_exists.assert_called_once_with(page, identity.po_number)
    agn_invoices.click_into_po.assert_called_once_with(page, identity.po_number)
    approve.assert_called_once_with(
        page,
        identity.vin,
        request_permission=False,
        expected_po_number=identity.po_number,
        close_work_order=True,
        allow_open_if_closure_unavailable=False,
    )


def test_fieldpo_approver_rejects_subject_po_mismatch_before_live_action(monkeypatch):
    invoice, identity = _approval_values(subject_po="FPO9999999")
    agn_invoices, approve, identity_reader = _approval_dependencies(monkeypatch)

    with pytest.raises(ValueError, match="Email subject PO FPO9999999 does not match"):
        FieldPOBrowserApprover(MagicMock(), identity_reader).approve(invoice, identity)

    identity_reader.read_identity.assert_not_called()
    agn_invoices.approve_displayed_po.assert_not_called()


def test_fieldpo_approver_rejects_unconfirmed_work_order_closure(monkeypatch):
    invoice, identity = _approval_values()
    agn_invoices, approve, identity_reader = _approval_dependencies(monkeypatch)
    approve.return_value = False

    with pytest.raises(RuntimeError, match="were not both confirmed"):
        FieldPOBrowserApprover(MagicMock(), identity_reader).approve(invoice, identity)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"live_mva": "057935873"}, "does not match Sheet MVA"),
        ({"live_vin": "1FMDE7BH9TLA47847"}, "does not match invoice/validated VIN"),
        ({"live_po": "FPO9999999"}, "changed from validated PO"),
        ({"live_amount": Decimal("341.00")}, "does not match invoice amount"),
        ({"creator": "Another User"}, "Created By must be Steele, Dirk"),
        ({"status": "APPROVED"}, "expected IN PROGRESS"),
        ({"amount": "341.00"}, "does not match invoice amount"),
    ],
)
def test_fieldpo_approver_blocks_when_live_gate_fails(monkeypatch, overrides, expected):
    invoice, identity = _approval_values()
    agn_invoices, approve, identity_reader = _approval_dependencies(monkeypatch, **overrides)

    with pytest.raises((ValueError, LookupError), match=expected):
        FieldPOBrowserApprover(MagicMock(), identity_reader).approve(invoice, identity)

    approve.assert_not_called()


@pytest.mark.parametrize("status", ["APPROVED", "IN PROGRESS"])
def test_review_reader_reads_status_only_from_exact_po_card(status):
    reader, page, identity_reader = _reader(
        f"PO # FPO1131464\n{status}\nAuthorized Amount\n$340.00"
    )

    snapshot = reader.read_review("FPO1131464")

    assert snapshot.po_status == status
    assert snapshot.authorized_amount == Decimal("340.00")
    identity_reader.read_identity.assert_called_once_with("FPO1131464")
    page.get_by_text.assert_called_once_with("FPO1131464", exact=True)


@pytest.mark.parametrize(
    "card_text",
    [
        "PO # FPO1131464\nUNKNOWN\nAuthorized Amount\n$340.00",
        "PO # FPO1131464\nAPPROVED\nIN PROGRESS\nAuthorized Amount\n$340.00",
    ],
)
def test_review_reader_rejects_unknown_or_ambiguous_status(card_text):
    reader, _, _ = _reader(card_text)

    with pytest.raises(ValueError, match="exactly one recognized FieldPO PO status"):
        reader.read_review("FPO1131464")