from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from invoice_workflow.contracts import (
    CaptureFailure,
    IdentityResult,
    OutlookScanResult,
    ParsedInvoice,
    StageStatus,
    ValidatedIdentity,
)
from invoice_workflow.coordinator import CompletedInvoicesCoordinator
from invoice_workflow.run_logging import WorkflowRunLogger
from invoice_workflow.store import InvoiceRepository


def _invoice(message_id="<message@example>", invoice_number="5294412"):
    return ParsedInvoice(
        internet_message_id=message_id,
        entry_id="entry",
        invoice_number=invoice_number,
        subject=f"[External] Invoice #{invoice_number} (PO # FPO1131464)",
        received_at=datetime(2026, 9, 3, tzinfo=UTC),
        subject_po_number="FPO1131464",
        pdf_po_number="FPO1131464",
        vin="1C4SJSBP6SS537975",
        invoice_amount=Decimal("340.00"),
        pdf_path="invoice.pdf",
    )


def _identity(invoice):
    return ValidatedIdentity(
        internet_message_id=invoice.internet_message_id,
        invoice_number=invoice.invoice_number,
        po_number=invoice.subject_po_number,
        vin=invoice.vin,
        mva="061093642",
        invoice_amount=invoice.invoice_amount,
        authorized_amount=invoice.invoice_amount,
    )


def test_live_run_captures_and_records_exact_identity(tmp_path):
    invoice = _invoice()
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((invoice,), ())
    resolver = MagicMock()
    resolver.resolve.return_value = IdentityResult(
        StageStatus.COMPLETE,
        identity=_identity(invoice),
    )
    repository = InvoiceRepository(tmp_path / "workflow.db")
    with WorkflowRunLogger(tmp_path / "logs", "run-1") as logger:
        coordinator = CompletedInvoicesCoordinator(
            outlook, resolver, repository, logger, run_id="run-1"
        )

        summary = coordinator.run_live(max_invoices=1)

    assert summary.exit_code == 0
    assert summary.identities_complete == 1
    assert summary.needs_review_moved == 0
    assert summary.needs_review_failed == 0
    state = repository.get_invoice(invoice.internet_message_id)
    assert state.identity_status is StageStatus.COMPLETE
    assert state.mva == "061093642"
    assert state.compass_status is StageStatus.PENDING
    assert state.po_status is StageStatus.PENDING
    assert state.work_order_status is StageStatus.PENDING
    assert state.outlook_status is StageStatus.COMPLETE
    outlook.move_to_processed.assert_called_once_with("<message@example>")
    outlook.move_to_needs_review.assert_not_called()


def test_capture_failure_returns_review_exit_code(tmp_path):
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult(
        (),
        (CaptureFailure("entry", "bad invoice", "missing PDF"),),
    )
    repository = InvoiceRepository(tmp_path / "workflow.db")
    with WorkflowRunLogger(tmp_path / "logs", "run-2") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook, MagicMock(), repository, logger, run_id="run-2"
        ).run_live()

    assert summary.exit_code == 2
    assert summary.capture_failures == 1
    assert len(summary.failures) == 1
    assert summary.failures[0].stage == "capture"
    assert summary.failures[0].code == "CAPTURE_FAILED"
    assert summary.needs_review_moved == 0
    assert summary.needs_review_failed == 0


def test_requested_invoice_not_found_returns_review_exit_code(tmp_path):
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((), ())
    repository = InvoiceRepository(tmp_path / "workflow.db")
    with WorkflowRunLogger(tmp_path / "logs", "run-not-found") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook, MagicMock(), repository, logger, run_id="run-not-found"
        ).run_live(invoice_number="5102081", max_invoices=1)

    assert summary.exit_code == 2
    assert summary.captured == 0
    outlook.scan_parent_invoices.assert_called_once_with(
        invoice_number="5102081",
        max_invoices=1,
        excluded_message_ids=frozenset(),
    )


def test_normal_batch_passes_terminal_identity_ids_to_outlook(tmp_path):
    completed = _invoice("<complete@example>", "5102080")
    repository = InvoiceRepository(tmp_path / "workflow.db")
    repository.initialize()
    repository.upsert_invoice(completed)
    attempt_id = repository.start_attempt(
        completed.internet_message_id,
        "previous-run",
        "identity",
    )
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((), ())

    with WorkflowRunLogger(tmp_path / "logs", "run-next") as logger:
        CompletedInvoicesCoordinator(
            outlook, MagicMock(), repository, logger, run_id="run-next"
        ).run_live(max_invoices=10)

    outlook.scan_parent_invoices.assert_called_once_with(
        invoice_number=None,
        max_invoices=10,
        excluded_message_ids=frozenset({"<complete@example>"}),
    )


def test_duplicate_invoice_number_blocks_both_without_resolution(tmp_path):
    first = _invoice("<first@example>")
    second = _invoice("<second@example>")
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((first, second), ())
    resolver = MagicMock()
    repository = InvoiceRepository(tmp_path / "workflow.db")
    with WorkflowRunLogger(tmp_path / "logs", "run-3") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook, resolver, repository, logger, run_id="run-3"
        ).run_live()

    assert summary.exit_code == 2
    assert summary.identities_blocked == 2
    assert summary.outlook_moved == 0
    assert summary.outlook_failed == 0
    assert summary.needs_review_moved == 2
    assert summary.needs_review_failed == 0
    resolver.resolve.assert_not_called()
    outlook.move_to_processed.assert_not_called()
    assert outlook.move_to_needs_review.call_count == 2
    assert repository.get_invoice(first.internet_message_id).identity_status is StageStatus.BLOCKED
    assert repository.get_invoice(second.internet_message_id).identity_status is StageStatus.BLOCKED


def test_live_run_moves_only_successful_validated_invoice(tmp_path):
    first = _invoice("<first@example>", "5294412")
    second = _invoice("<second@example>", "5294413")
    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((first, second), ())
    resolver = MagicMock()
    resolver.resolve.side_effect = [
        IdentityResult(StageStatus.COMPLETE, identity=_identity(first)),
        IdentityResult(StageStatus.BLOCKED, detail="VIN mismatch"),
    ]
    repository = InvoiceRepository(tmp_path / "workflow.db")

    with WorkflowRunLogger(tmp_path / "logs", "run-live") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook, resolver, repository, logger, run_id="run-live"
        ).run_live()

    assert summary.exit_code == 2
    assert summary.identities_complete == 1
    assert summary.identities_blocked == 1
    assert summary.outlook_moved == 1
    assert summary.outlook_failed == 0
    assert summary.needs_review_moved == 1
    assert summary.needs_review_failed == 0
    assert len(summary.failures) == 1
    assert summary.failures[0].code == "VIN_MISMATCH"
    outlook.move_to_processed.assert_called_once_with("<first@example>")
    outlook.move_to_needs_review.assert_called_once_with("<second@example>")

    first_state = repository.get_invoice(first.internet_message_id)
    second_state = repository.get_invoice(second.internet_message_id)
    assert first_state is not None
    assert second_state is not None
    assert first_state.identity_status is StageStatus.COMPLETE
    assert first_state.outlook_status is StageStatus.COMPLETE
    assert second_state.identity_status is StageStatus.BLOCKED
    assert second_state.outlook_status is StageStatus.PENDING


def test_live_run_blocks_terminal_message_id_when_invoice_number_bypasses_exclusion(tmp_path):
    invoice = _invoice("<terminal@example>", "5294412")
    repository = InvoiceRepository(tmp_path / "workflow.db")
    repository.initialize()
    repository.upsert_invoice(invoice)
    attempt_id = repository.start_attempt(
        invoice.internet_message_id,
        "previous-run",
        "identity",
    )
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((invoice,), ())
    resolver = MagicMock()

    with WorkflowRunLogger(tmp_path / "logs", "run-live-terminal") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook,
            resolver,
            repository,
            logger,
            run_id="run-live-terminal",
        ).run_live(invoice_number="5294412", max_invoices=1)

    assert summary.exit_code == 2
    assert summary.identities_complete == 0
    assert summary.identities_blocked == 1
    assert summary.outlook_moved == 0
    assert summary.outlook_failed == 0
    assert summary.needs_review_moved == 1
    assert summary.needs_review_failed == 0
    assert len(summary.failures) == 1
    assert summary.failures[0].code == "TERMINAL_MESSAGE_ID"
    resolver.resolve.assert_not_called()
    outlook.move_to_processed.assert_not_called()
    outlook.move_to_needs_review.assert_called_once_with("<terminal@example>")


def test_live_run_blocks_terminal_message_id_with_terminal_identity_when_invoice_number_bypasses_exclusion(
    tmp_path,
):
    invoice = _invoice("<terminal@example>", "5294412")
    repository = InvoiceRepository(tmp_path / "workflow.db")
    repository.initialize()
    repository.upsert_invoice(invoice)
    attempt_id = repository.start_attempt(
        invoice.internet_message_id,
        "previous-run",
        "identity",
    )
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    outlook = MagicMock()
    outlook.scan_parent_invoices.return_value = OutlookScanResult((invoice,), ())
    resolver = MagicMock()

    with WorkflowRunLogger(tmp_path / "logs", "run-dry-terminal") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook,
            resolver,
            repository,
            logger,
            run_id="run-dry-terminal",
        ).run_live(invoice_number="5294412", max_invoices=1)

    assert summary.exit_code == 2
    assert summary.identities_complete == 0
    assert summary.identities_blocked == 1
    assert summary.outlook_moved == 0
    assert summary.outlook_failed == 0
    assert summary.needs_review_moved == 1
    assert summary.needs_review_failed == 0
    resolver.resolve.assert_not_called()
    outlook.move_to_processed.assert_not_called()
    outlook.move_to_needs_review.assert_called_once_with("<terminal@example>")


def test_terminal_sweep_moves_terminal_ids_to_needs_review_with_cap(tmp_path):
    first = _invoice("<first@example>", "5294001")
    second = _invoice("<second@example>", "5294002")
    repository = InvoiceRepository(tmp_path / "workflow.db")
    repository.initialize()
    for invoice in (first, second):
        repository.upsert_invoice(invoice)
        attempt_id = repository.start_attempt(
            invoice.internet_message_id,
            "previous-run",
            "identity",
        )
        repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    outlook = MagicMock()
    with WorkflowRunLogger(tmp_path / "logs", "run-sweep") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook,
            MagicMock(),
            repository,
            logger,
            run_id="run-sweep",
        ).run_sweep_terminal_to_needs_review(max_invoices=1)

    assert summary.exit_code == 0
    assert summary.outlook_moved == 0
    assert summary.outlook_failed == 0
    assert summary.needs_review_moved == 1
    assert summary.needs_review_failed == 0
    outlook.move_to_needs_review.assert_called_once_with("<first@example>")


def test_terminal_sweep_skips_missing_message_ids_without_failure(tmp_path):
    invoice = _invoice("<missing@example>", "5295001")
    repository = InvoiceRepository(tmp_path / "workflow.db")
    repository.initialize()
    repository.upsert_invoice(invoice)
    attempt_id = repository.start_attempt(
        invoice.internet_message_id,
        "previous-run",
        "identity",
    )
    repository.finish_attempt(attempt_id, StageStatus.COMPLETE)

    outlook = MagicMock()
    outlook.move_to_needs_review.side_effect = LookupError(
        "Exact InternetMessageID is not present in this run: <missing@example>"
    )

    with WorkflowRunLogger(tmp_path / "logs", "run-sweep-missing") as logger:
        summary = CompletedInvoicesCoordinator(
            outlook,
            MagicMock(),
            repository,
            logger,
            run_id="run-sweep-missing",
        ).run_sweep_terminal_to_needs_review()

    assert summary.exit_code == 0
    assert summary.needs_review_moved == 0
    assert summary.needs_review_failed == 0
    assert summary.failures == ()
