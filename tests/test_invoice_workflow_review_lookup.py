from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from invoice_workflow import review_lookup
from invoice_workflow.contracts import (
    CaptureFailure,
    OutlookScanResult,
    ParsedInvoice,
    Stage,
    StageStatus,
)
from invoice_workflow.coordinator import CompletedInvoicesCoordinator
from invoice_workflow.run_logging import WorkflowRunLogger
from invoice_workflow.store import InvoiceRepository


def _invoice(message_id: str, *, amount: str = "340.00") -> ParsedInvoice:
    return ParsedInvoice(
        internet_message_id=message_id,
        entry_id=f"entry-{message_id}",
        invoice_number="5546656",
        subject="[External] Invoice #5546656 (PO # FPO1197467)",
        received_at=datetime(2026, 11, 12, tzinfo=UTC),
        subject_po_number="FPO1197467",
        pdf_po_number="FPO1197467",
        vin="1HGCY1F20SA089636",
        invoice_amount=Decimal(amount),
        pdf_path="invoice.pdf",
    )


def test_po_lookup_reports_each_matching_record_and_reason(
    tmp_path,
    monkeypatch,
    capsys,
):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    first = _invoice("message-1", amount="365.00")
    repository.upsert_invoice(first)
    attempt_id = repository.start_attempt(
        first.internet_message_id,
        "run-1",
        Stage.IDENTITY,
    )
    repository.finish_attempt(
        attempt_id,
        StageStatus.BLOCKED,
        "Authorized amount mismatch: invoice 365.00, FieldPO 340.00",
    )
    repository.upsert_invoice(replace(_invoice("message-2"), received_at=datetime(
        2026, 11, 13, tzinfo=UTC
    )))
    repository.block_duplicate_invoice_numbers()
    monkeypatch.setattr(review_lookup, "DATABASE_PATH", repository.database_path)

    result = review_lookup.main(["--po", "FPO1197467"])
    output = capsys.readouterr().out

    assert result == 0
    assert "2 matching record(s)" in output
    assert "Authorized amount mismatch: invoice 365.00, FieldPO 340.00" in output
    assert "Duplicate invoice number: 5546656" in output


def test_lookup_cli_reports_capture_reason_without_invoice_row(
    tmp_path,
    monkeypatch,
    capsys,
):
    outlook = MagicMock()
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    failure = CaptureFailure(
        entry_id="capture-entry",
        subject="[External] Invoice #5546656 (PO # FPO1197467)",
        detail="Expected exactly one PDF attachment, found 2",
        internet_message_id="capture-message",
    )
    with WorkflowRunLogger(tmp_path / "logs", "capture") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook,
            MagicMock(),
            repository,
            logger,
            run_id="capture",
        ).run_captured(OutlookScanResult((), (failure,)))
    assert summary.needs_review_moved == 1
    assert repository.get_invoice("capture-message") is None
    monkeypatch.setattr(review_lookup, "DATABASE_PATH", repository.database_path)

    result = review_lookup.main(["--po", "FPO1197467"])
    output = capsys.readouterr().out
    assert result == 0
    assert "Review stage/status: capture/blocked" in output
    assert "Review code: CAPTURE_FAILED" in output
    assert "Expected exactly one PDF attachment, found 2" in output
    assert "capture-message" not in output

    result = review_lookup.main(["--po", "FPO9999999"])
    output = capsys.readouterr().out
    assert result == 1
    assert "No ledger records found for PO FPO9999999." in output
    assert "Try a different MVA, VIN, or PO" in output
    assert len(repository.find_review_records("FPO1197467")) == 1


def test_lookup_cli_displays_failure_detail_without_message_ids(
    tmp_path,
    monkeypatch,
    capsys,
):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    invoice = _invoice("private-message-id")
    repository.upsert_invoice(invoice)
    attempt_id = repository.start_attempt(
        invoice.internet_message_id,
        "run-1",
        Stage.IDENTITY,
    )
    repository.finish_attempt(
        attempt_id,
        StageStatus.BLOCKED,
        "Attempt detail that is not the reason for the review move",
    )
    repository.record_review_event(
        invoice_number=invoice.invoice_number,
        internet_message_id=invoice.internet_message_id,
        run_id="run-1",
        stage="identity",
        code="VIN_MISMATCH",
        detail="FieldPO VIN mismatch: expected VIN-A, found VIN-B",
        subject_po_number=invoice.subject_po_number,
        pdf_po_number=invoice.pdf_po_number,
        vin=invoice.vin,
        mva="060062262",
    )
    monkeypatch.setattr(review_lookup, "DATABASE_PATH", repository.database_path)

    result = review_lookup.main(["--vin", invoice.vin])

    output = capsys.readouterr().out
    assert result == 0
    assert "Review code: VIN_MISMATCH" in output
    assert "Review reason: FieldPO VIN mismatch: expected VIN-A, found VIN-B" in output
    assert "Attempt detail that is not the reason for the review move" not in output
    assert "private-message-id" not in output


def test_lookup_cli_matches_vin_po_and_either_mva_format(
    tmp_path,
    monkeypatch,
    capsys,
):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    invoice = _invoice("message-identifiers")
    repository.upsert_invoice(invoice)
    attempt_id = repository.start_attempt(
        invoice.internet_message_id,
        "run-1",
        Stage.IDENTITY,
    )
    repository.finish_attempt(
        attempt_id,
        StageStatus.BLOCKED,
        "FieldPO MVA mismatch: expected 060062262, found 060062263",
        mva="060062262",
    )
    monkeypatch.setattr(review_lookup, "DATABASE_PATH", repository.database_path)

    for identifier, expected_match in (
        (["--vin", "1HGCY1F20SA089636"], "VIN"),
        (["--po", "FPO1197467"], "PO"),
        (["--mva", "60062262"], "MVA"),
        (["--mva", "060062262"], "MVA"),
        (["--mva", "60062263"], "MVA"),
    ):
        result = review_lookup.main(identifier)
        output = capsys.readouterr().out

        assert result == 0
        assert f"Matched by: {expected_match}" in output

        assert "FieldPO MVA mismatch" in output

    with pytest.raises(SystemExit) as error:
        review_lookup.main(["5546656"])

    assert error.value.code == 2
    assert "one of the arguments --mva --vin --po is required" in (
        capsys.readouterr().err
    )
