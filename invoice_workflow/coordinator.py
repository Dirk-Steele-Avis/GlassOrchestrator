"""Synchronous coordinator for the completed-invoice workflow."""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import uuid4

from invoice_workflow.contracts import (
    IdentityResolver,
    OutlookSource,
    Stage,
    StageStatus,
)
from invoice_workflow.run_logging import WorkflowRunLogger
from invoice_workflow.store import InvoiceRepository


_INVOICE_NUMBER_PATTERN = re.compile(r"\bInvoice\s+#(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FailureRecord:
    run_id: str
    invoice_number: str
    stage: str
    code: str
    detail: str
    subject: str = ""
    subject_po_number: str = ""
    pdf_po_number: str = ""
    vin: str = ""
    invoice_amount: str = ""


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    captured: int
    capture_failures: int
    identities_complete: int
    identities_blocked: int
    outlook_moved: int
    outlook_failed: int
    needs_review_moved: int
    needs_review_failed: int
    failures: tuple[FailureRecord, ...]
    exit_code: int


class CompletedInvoicesCoordinator:
    """Coordinate capture and strict validation before mutation stages."""

    def __init__(
        self,
        outlook_source: OutlookSource,
        identity_resolver: IdentityResolver,
        repository: InvoiceRepository,
        logger: WorkflowRunLogger,
        *,
        run_id: str | None = None,
    ) -> None:
        self._outlook_source = outlook_source
        self._identity_resolver = identity_resolver
        self._repository = repository
        self._logger = logger
        self._run_id = run_id or uuid4().hex

    def run_live(
        self,
        *,
        invoice_number: str | None = None,
        max_invoices: int | None = None,
    ) -> RunSummary:
        self._repository.initialize()
        self._logger.info("live_run_started")
        terminal_message_ids = frozenset(
            self._repository.list_terminal_identity_message_ids()
        )
        excluded_message_ids = (
            frozenset()
            if invoice_number is not None
            else terminal_message_ids
        )
        scan = self._outlook_source.scan_parent_invoices(
            invoice_number=invoice_number,
            max_invoices=max_invoices,
            excluded_message_ids=excluded_message_ids,
        )
        failures: list[FailureRecord] = []
        outlook_moved = 0
        outlook_failed = 0
        needs_review_moved = 0
        needs_review_failed = 0
        self._logger.info(
            f"capture_complete invoices={len(scan.invoices)} failures={len(scan.failures)}",
            stage="capture",
        )
        target_not_found = invoice_number is not None and not scan.invoices
        if target_not_found:
            detail = f"Requested invoice {invoice_number} was not found in capture results"
            code = "INVOICE_NOT_FOUND"
            failures.append(
                FailureRecord(
                    run_id=self._run_id,
                    invoice_number=invoice_number,
                    stage="capture",
                    code=code,
                    detail=detail,
                )
            )
            self._logger.error(
                f"invoice_not_found code={code} invoice_number={invoice_number}",
                invoice=invoice_number,
                stage="capture",
            )
        for failure in scan.failures:
            invoice_from_subject = _invoice_number_from_subject(failure.subject)
            code = _classify_failure("capture", failure.detail)
            failures.append(
                FailureRecord(
                    run_id=self._run_id,
                    invoice_number=invoice_from_subject,
                    stage="capture",
                    code=code,
                    detail=failure.detail,
                    subject=failure.subject,
                )
            )
            self._logger.error(
                f"capture_failed code={code} entry_id={failure.entry_id} detail={failure.detail}",
                stage="capture",
            )
            moved, failed = self._move_reviewed_message(
                invoice_from_subject,
                failure.internet_message_id,
                failures,
                detail_context="capture_failure",
                moved_count=needs_review_moved,
                failed_count=needs_review_failed,
            )
            needs_review_moved = moved
            needs_review_failed = failed
        for invoice in scan.invoices:
            self._repository.upsert_invoice(invoice)

        duplicate_numbers = set(self._repository.block_duplicate_invoice_numbers())
        identities_complete = 0
        identities_blocked = 0
        for invoice in scan.invoices:
            if invoice.internet_message_id in terminal_message_ids:
                identities_blocked += 1
                detail = "Already terminal by exact InternetMessageID"
                code = "TERMINAL_MESSAGE_ID"
                failures.append(
                    FailureRecord(
                        run_id=self._run_id,
                        invoice_number=invoice.invoice_number,
                        stage=str(Stage.IDENTITY),
                        code=code,
                        detail=detail,
                        subject=invoice.subject,
                        subject_po_number=invoice.subject_po_number,
                        pdf_po_number=invoice.pdf_po_number,
                        vin=invoice.vin,
                        invoice_amount=str(invoice.invoice_amount),
                    )
                )
                self._logger.error(
                    f"identity_blocked code={code} detail={detail}",
                    invoice=invoice.invoice_number,
                    stage=Stage.IDENTITY,
                )
                moved, failed = self._move_reviewed_message(
                    invoice.invoice_number,
                    invoice.internet_message_id,
                    failures,
                    detail_context="terminal_message_id",
                    moved_count=needs_review_moved,
                    failed_count=needs_review_failed,
                )
                needs_review_moved, needs_review_failed = moved, failed
                continue

            if invoice.invoice_number in duplicate_numbers:
                identities_blocked += 1
                detail = "Duplicate invoice number in repository"
                code = "DUPLICATE_INVOICE_NUMBER"
                failures.append(
                    FailureRecord(
                        run_id=self._run_id,
                        invoice_number=invoice.invoice_number,
                        stage=str(Stage.IDENTITY),
                        code=code,
                        detail=detail,
                        subject=invoice.subject,
                        subject_po_number=invoice.subject_po_number,
                        pdf_po_number=invoice.pdf_po_number,
                        vin=invoice.vin,
                        invoice_amount=str(invoice.invoice_amount),
                    )
                )
                self._logger.error(
                    f"identity_blocked code={code} detail={detail}",
                    invoice=invoice.invoice_number,
                    stage=Stage.IDENTITY,
                )
                moved, failed = self._move_reviewed_message(
                    invoice.invoice_number,
                    invoice.internet_message_id,
                    failures,
                    detail_context="duplicate_invoice_number",
                    moved_count=needs_review_moved,
                    failed_count=needs_review_failed,
                )
                needs_review_moved = moved
                needs_review_failed = failed
                continue

            attempt_id = self._repository.start_attempt(
                invoice.internet_message_id,
                self._run_id,
                Stage.IDENTITY,
            )
            try:
                result = self._identity_resolver.resolve(invoice)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._repository.finish_attempt(
                    attempt_id,
                    StageStatus.FAILED,
                    detail,
                )
                identities_blocked += 1
                code = "IDENTITY_EXCEPTION"
                failures.append(
                    FailureRecord(
                        run_id=self._run_id,
                        invoice_number=invoice.invoice_number,
                        stage=str(Stage.IDENTITY),
                        code=code,
                        detail=detail,
                        subject=invoice.subject,
                        subject_po_number=invoice.subject_po_number,
                        pdf_po_number=invoice.pdf_po_number,
                        vin=invoice.vin,
                        invoice_amount=str(invoice.invoice_amount),
                    )
                )
                self._logger.exception(
                    f"identity_failed code={code} detail={detail}",
                    invoice=invoice.invoice_number,
                    stage=Stage.IDENTITY,
                )
                moved, failed = self._move_reviewed_message(
                    invoice.invoice_number,
                    invoice.internet_message_id,
                    failures,
                    detail_context="identity_exception",
                    moved_count=needs_review_moved,
                    failed_count=needs_review_failed,
                )
                needs_review_moved = moved
                needs_review_failed = failed
                continue

            identity = result.identity
            self._repository.finish_attempt(
                attempt_id,
                result.status,
                result.detail,
                mva=identity.mva if identity else None,
                authorized_amount=(
                    str(identity.authorized_amount) if identity else None
                ),
            )
            if result.status is StageStatus.COMPLETE and identity is not None:
                identities_complete += 1
                self._logger.info(
                    "identity_complete "
                    f"po={identity.po_number} vin={identity.vin} "
                    f"mva={identity.mva} amount={identity.invoice_amount}",
                    invoice=invoice.invoice_number,
                    stage=Stage.IDENTITY,
                )
                outlook_attempt_id = self._repository.start_attempt(
                    invoice.internet_message_id,
                    self._run_id,
                    Stage.OUTLOOK,
                )
                try:
                    self._outlook_source.move_to_processed(invoice.internet_message_id)
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    self._repository.finish_attempt(
                        outlook_attempt_id,
                        StageStatus.FAILED,
                        detail,
                    )
                    outlook_failed += 1
                    code = "OUTLOOK_MOVE_FAILED"
                    failures.append(
                        FailureRecord(
                            run_id=self._run_id,
                            invoice_number=invoice.invoice_number,
                            stage=str(Stage.OUTLOOK),
                            code=code,
                            detail=detail,
                            subject=invoice.subject,
                            subject_po_number=invoice.subject_po_number,
                            pdf_po_number=invoice.pdf_po_number,
                            vin=invoice.vin,
                            invoice_amount=str(invoice.invoice_amount),
                        )
                    )
                    self._logger.exception(
                        f"outlook_failed code={code} detail={detail}",
                        invoice=invoice.invoice_number,
                        stage=Stage.OUTLOOK,
                    )
                else:
                    self._repository.finish_attempt(
                        outlook_attempt_id,
                        StageStatus.COMPLETE,
                    )
                    outlook_moved += 1
                    self._logger.info(
                        "outlook_complete moved_to_processed",
                        invoice=invoice.invoice_number,
                        stage=Stage.OUTLOOK,
                    )
            else:
                identities_blocked += 1
                code = _classify_failure(str(Stage.IDENTITY), result.detail)
                failures.append(
                    FailureRecord(
                        run_id=self._run_id,
                        invoice_number=invoice.invoice_number,
                        stage=str(Stage.IDENTITY),
                        code=code,
                        detail=result.detail,
                        subject=invoice.subject,
                        subject_po_number=invoice.subject_po_number,
                        pdf_po_number=invoice.pdf_po_number,
                        vin=invoice.vin,
                        invoice_amount=str(invoice.invoice_amount),
                    )
                )
                self._logger.error(
                    f"identity_{result.status} code={code} detail={result.detail}",
                    invoice=invoice.invoice_number,
                    stage=Stage.IDENTITY,
                )
                moved, failed = self._move_reviewed_message(
                    invoice.invoice_number,
                    invoice.internet_message_id,
                    failures,
                    detail_context="identity_blocked",
                    moved_count=needs_review_moved,
                    failed_count=needs_review_failed,
                )
                needs_review_moved = moved
                needs_review_failed = failed

        exit_code = (
            2
            if scan.failures
            or identities_blocked
            or outlook_failed
            or needs_review_failed
            or target_not_found
            else 0
        )
        self._logger.info(
            "live_run_complete "
            f"captured={len(scan.invoices)} identity_complete={identities_complete} "
            f"identity_blocked={identities_blocked} "
            f"outlook_moved={outlook_moved} outlook_failed={outlook_failed} "
            f"needs_review_moved={needs_review_moved} "
            f"needs_review_failed={needs_review_failed} "
            f"exit_code={exit_code}"
        )
        return RunSummary(
            run_id=self._run_id,
            captured=len(scan.invoices),
            capture_failures=len(scan.failures),
            identities_complete=identities_complete,
            identities_blocked=identities_blocked,
            outlook_moved=outlook_moved,
            outlook_failed=outlook_failed,
            needs_review_moved=needs_review_moved,
            needs_review_failed=needs_review_failed,
            failures=tuple(failures),
            exit_code=exit_code,
        )

    def run_sweep_terminal_to_needs_review(
        self,
        *,
        max_invoices: int | None = None,
    ) -> RunSummary:
        self._repository.initialize()
        self._logger.info("terminal_sweep_started")
        terminal_message_ids = list(
            self._repository.list_terminal_identity_message_ids()
        )
        if max_invoices is not None:
            terminal_message_ids = terminal_message_ids[:max_invoices]

        failures: list[FailureRecord] = []
        needs_review_moved = 0
        needs_review_failed = 0
        for internet_message_id in terminal_message_ids:
            moved, failed = self._move_reviewed_message_sweep(
                internet_message_id,
                failures,
                moved_count=needs_review_moved,
                failed_count=needs_review_failed,
            )
            needs_review_moved, needs_review_failed = moved, failed

        exit_code = 2 if needs_review_failed else 0
        self._logger.info(
            "terminal_sweep_complete "
            f"candidates={len(terminal_message_ids)} "
            f"needs_review_moved={needs_review_moved} "
            f"needs_review_failed={needs_review_failed} "
            f"exit_code={exit_code}"
        )
        return RunSummary(
            run_id=self._run_id,
            captured=0,
            capture_failures=0,
            identities_complete=0,
            identities_blocked=0,
            outlook_moved=0,
            outlook_failed=0,
            needs_review_moved=needs_review_moved,
            needs_review_failed=needs_review_failed,
            failures=tuple(failures),
            exit_code=exit_code,
        )

    def _move_reviewed_message_sweep(
        self,
        internet_message_id: str,
        failures: list[FailureRecord],
        *,
        moved_count: int,
        failed_count: int,
    ) -> tuple[int, int]:
        try:
            self._outlook_source.move_to_needs_review(internet_message_id)
        except LookupError as exc:
            detail = str(exc)
            if "Exact InternetMessageID is not present in this run" in detail:
                self._logger.info(
                    "outlook_skip terminal_not_in_invoice "
                    f"internet_message_id={internet_message_id}",
                    stage=Stage.OUTLOOK,
                )
                return moved_count, failed_count
            failure_detail = (
                f"LookupError: {detail} while moving reviewed terminal_sweep message"
            )
            failures.append(
                FailureRecord(
                    run_id=self._run_id,
                    invoice_number="-",
                    stage=str(Stage.OUTLOOK),
                    code="OUTLOOK_MOVE_FAILED",
                    detail=failure_detail,
                )
            )
            self._logger.exception(
                f"outlook_failed code=OUTLOOK_MOVE_FAILED detail={failure_detail}",
                invoice="-",
                stage=Stage.OUTLOOK,
            )
            return moved_count, failed_count + 1
        except Exception as exc:
            detail = (
                f"{type(exc).__name__}: {exc} "
                "while moving reviewed terminal_sweep message"
            )
            failures.append(
                FailureRecord(
                    run_id=self._run_id,
                    invoice_number="-",
                    stage=str(Stage.OUTLOOK),
                    code="OUTLOOK_MOVE_FAILED",
                    detail=detail,
                )
            )
            self._logger.exception(
                f"outlook_failed code=OUTLOOK_MOVE_FAILED detail={detail}",
                invoice="-",
                stage=Stage.OUTLOOK,
            )
            return moved_count, failed_count + 1
        self._logger.info(
            "outlook_complete moved_to_needs_review reviewed=terminal_sweep",
            invoice="-",
            stage=Stage.OUTLOOK,
        )
        return moved_count + 1, failed_count

    def _move_reviewed_message(
        self,
        invoice_number: str,
        internet_message_id: str,
        failures: list[FailureRecord],
        *,
        detail_context: str,
        moved_count: int = 0,
        failed_count: int = 0,
    ) -> tuple[int, int]:
        if not internet_message_id:
            return moved_count, failed_count
        try:
            self._outlook_source.move_to_needs_review(internet_message_id)
        except Exception as exc:
            detail = (
                f"{type(exc).__name__}: {exc}"
                f" while moving reviewed {detail_context} message"
            )
            failures.append(
                FailureRecord(
                    run_id=self._run_id,
                    invoice_number=invoice_number or "-",
                    stage=str(Stage.OUTLOOK),
                    code="OUTLOOK_MOVE_FAILED",
                    detail=detail,
                )
            )
            self._logger.exception(
                f"outlook_failed code=OUTLOOK_MOVE_FAILED detail={detail}",
                invoice=invoice_number,
                stage=Stage.OUTLOOK,
            )
            return moved_count, failed_count + 1
        self._logger.info(
            f"outlook_complete moved_to_needs_review reviewed={detail_context}",
            invoice=invoice_number,
            stage=Stage.OUTLOOK,
        )
        return moved_count + 1, failed_count


def _invoice_number_from_subject(subject: str) -> str:
    match = _INVOICE_NUMBER_PATTERN.search(subject)
    return match.group(1) if match else "-"


def _classify_failure(stage: str, detail: str) -> str:
    normalized = detail.lower()
    if "subject/pdf po mismatch" in normalized:
        return "PO_SUBJECT_PDF_MISMATCH"
    if "subject does not contain one exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "pdf does not contain one exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "expected exactly one pdf po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "invalid exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "vin mismatch" in normalized:
        return "VIN_MISMATCH"
    if "authorized amount mismatch" in normalized:
        return "AMOUNT_MISMATCH"
    if "mva mismatch" in normalized:
        return "MVA_MISMATCH"
    if stage == "capture":
        return "CAPTURE_FAILED"
    if stage == str(Stage.OUTLOOK):
        return "OUTLOOK_FAILED"
    return "IDENTITY_FAILED"
