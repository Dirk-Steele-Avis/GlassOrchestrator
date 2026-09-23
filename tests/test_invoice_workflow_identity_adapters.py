from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from invoice_workflow.contracts import FieldPOIdentitySnapshot
from invoice_workflow.identity_adapters import FieldPOBrowserReviewReader


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