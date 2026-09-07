from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from invoice_workflow.contracts import ParsedInvoice, Stage, StageStatus
from invoice_workflow.store import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    InvoiceRepository,
)


def _invoice(message_id: str, invoice_number: str) -> ParsedInvoice:
    return ParsedInvoice(
        internet_message_id=message_id,
        entry_id=f"entry-{message_id}",
        invoice_number=invoice_number,
        subject=f"[External] Invoice #{invoice_number} (PO # FPO1234567)",
        received_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        subject_po_number="FPO1234567",
        pdf_po_number="FPO1234567",
        vin="1FMDE7BH9TLA47847",
        invoice_amount=Decimal("340.00"),
        pdf_path=f"{invoice_number}.pdf",
    )


def test_initialize_configures_database_and_schema(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")

    repository.initialize()

    with repository.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_upsert_preserves_completed_stage_state(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    original = _invoice("message-1", "100")
    repository.upsert_invoice(original)
    attempt_id = repository.start_attempt("message-1", "run-1", Stage.COMPASS)
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    repository.upsert_invoice(
        replace(original, entry_id="new-entry", pdf_path="new.pdf")
    )

    state = repository.get_invoice("message-1")
    assert state is not None
    assert state.compass_status is StageStatus.COMPLETE


def test_duplicate_invoice_number_blocks_every_message(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    repository.upsert_invoice(_invoice("message-1", "100"))
    repository.upsert_invoice(_invoice("message-2", "100"))

    duplicates = repository.block_duplicate_invoice_numbers()

    assert duplicates == ("100",)
    assert repository.get_invoice("message-1").identity_status is StageStatus.BLOCKED
    assert repository.get_invoice("message-2").identity_status is StageStatus.BLOCKED


def test_attempt_transition_updates_invoice_and_keeps_history(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    repository.upsert_invoice(_invoice("message-1", "100"))

    attempt_id = repository.start_attempt("message-1", "run-1", Stage.IDENTITY)
    repository.finish_attempt(
        attempt_id,
        StageStatus.COMPLETE,
        mva="012345678",
        authorized_amount="340.00",
    )

    state = repository.get_invoice("message-1")
    attempts = repository.list_attempts("message-1")
    assert state is not None
    assert state.identity_status is StageStatus.COMPLETE
    assert state.mva == "012345678"
    assert state.authorized_amount == "340.00"
    assert attempts[0]["status"] == StageStatus.COMPLETE
    assert attempts[0]["completed_at"] is not None


def test_attempt_cannot_finish_twice(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    repository.upsert_invoice(_invoice("message-1", "100"))
    attempt_id = repository.start_attempt("message-1", "run-1", Stage.COMPASS)
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    with pytest.raises(RuntimeError, match="already complete"):
        repository.finish_attempt(attempt_id, StageStatus.COMPLETE)


def test_terminal_identity_ids_exclude_retryable_failures(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()
    for index, status in enumerate(
        (StageStatus.COMPLETE, StageStatus.BLOCKED, StageStatus.FAILED),
        start=1,
    ):
        message_id = f"message-{index}"
        repository.upsert_invoice(_invoice(message_id, str(100 + index)))
        attempt_id = repository.start_attempt(message_id, "run-1", Stage.IDENTITY)
        repository.finish_attempt(attempt_id, status, "test")
    repository.upsert_invoice(_invoice("message-4", "104"))

    assert repository.list_terminal_identity_message_ids() == (
        "message-1",
        "message-2",
    )


def test_start_attempt_rejects_unknown_message(tmp_path):
    repository = InvoiceRepository(tmp_path / "workflow.sqlite3")
    repository.initialize()

    with pytest.raises(KeyError, match="Unknown invoice message ID"):
        repository.start_attempt("missing", "run-1", Stage.COMPASS)
