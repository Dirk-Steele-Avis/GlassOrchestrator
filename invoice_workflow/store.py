"""SQLite repository for durable completed-invoice workflow state."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from invoice_workflow.contracts import ParsedInvoice, ReviewHistory, Stage, StageStatus

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000

_STAGE_COLUMNS = {
    Stage.IDENTITY: "identity_status",
    Stage.COMPASS: "compass_status",
    Stage.FIELDPO_PO: "po_status",
    Stage.FIELDPO_WORK_ORDER: "work_order_status",
    Stage.OUTLOOK: "outlook_status",
}


@dataclass(frozen=True, slots=True)
class InvoiceState:
    internet_message_id: str
    invoice_number: str
    subject_po_number: str
    pdf_po_number: str
    vin: str
    mva: str
    invoice_amount: str
    authorized_amount: str
    identity_status: StageStatus
    compass_status: StageStatus
    po_status: StageStatus
    work_order_status: StageStatus
    outlook_status: StageStatus
    last_error: str


@dataclass(frozen=True, slots=True)
class InvoiceLookupRecord:
    invoice_number: str
    received_at: str
    subject_po_number: str
    pdf_po_number: str
    vin: str
    mva: str
    invoice_amount: str
    identity_status: StageStatus
    outlook_status: StageStatus
    last_error: str


@dataclass(frozen=True, slots=True)
class InvoiceReviewLookup:
    invoice_number: str
    matched_by: tuple[str, ...]
    received_at: str
    pdf_po_number: str
    vin: str
    mva: str
    invoice_amount: str
    identity_status: StageStatus
    outlook_status: StageStatus
    review_history: ReviewHistory | None


class InvoiceRepository:
    """The only data-access layer for completed-invoice workflow state."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == SCHEMA_VERSION:
                return

            connection.execute("BEGIN IMMEDIATE")
            try:
                if version < 1:
                    self._apply_version_1(connection)
                if version < 2:
                    self._apply_version_2(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _apply_version_1(connection: sqlite3.Connection) -> None:
        allowed_statuses = "'pending','in_progress','complete','blocked','failed'"
        connection.execute(
            f"""
            CREATE TABLE invoices (
                internet_message_id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                invoice_number TEXT NOT NULL,
                subject TEXT NOT NULL,
                received_at TEXT NOT NULL,
                subject_po_number TEXT NOT NULL,
                pdf_po_number TEXT NOT NULL,
                vin TEXT NOT NULL,
                mva TEXT NOT NULL DEFAULT '',
                invoice_amount TEXT NOT NULL,
                authorized_amount TEXT NOT NULL DEFAULT '',
                pdf_path TEXT NOT NULL,
                identity_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (identity_status IN ({allowed_statuses})),
                compass_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (compass_status IN ({allowed_statuses})),
                po_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (po_status IN ({allowed_statuses})),
                work_order_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (work_order_status IN ({allowed_statuses})),
                outlook_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (outlook_status IN ({allowed_statuses})),
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                internet_message_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                stage TEXT NOT NULL
                    CHECK (stage IN ('identity','compass','fieldpo_po',
                                     'fieldpo_work_order','outlook')),
                status TEXT NOT NULL
                    CHECK (status IN ({allowed_statuses})),
                detail TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (internet_message_id)
                    REFERENCES invoices(internet_message_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_invoices_invoice_number ON invoices(invoice_number)"
        )
        connection.execute(
            "CREATE INDEX idx_invoices_statuses ON invoices("
            "identity_status, compass_status, po_status, work_order_status, outlook_status)"
        )
        connection.execute(
            "CREATE INDEX idx_attempts_message_stage "
            "ON attempts(internet_message_id, stage, id)"
        )

    @staticmethod
    def _apply_version_2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE invoice_review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL,
                internet_message_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                code TEXT NOT NULL,
                detail TEXT NOT NULL,
                subject_po_number TEXT NOT NULL DEFAULT '',
                pdf_po_number TEXT NOT NULL DEFAULT '',
                vin TEXT NOT NULL DEFAULT '',
                mva TEXT NOT NULL DEFAULT '',
                fieldpo_mva TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_invoice_review_events_invoice "
            "ON invoice_review_events(invoice_number, id)"
        )
        connection.execute(
            "CREATE INDEX idx_invoice_review_events_message "
            "ON invoice_review_events(internet_message_id, id)"
        )

    def upsert_invoice(self, invoice: ParsedInvoice) -> None:
        now = _utc_now()
        with self.connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO invoices (
                    internet_message_id, entry_id, invoice_number, subject,
                    received_at, subject_po_number, pdf_po_number, vin,
                    invoice_amount, pdf_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(internet_message_id) DO UPDATE SET
                    entry_id = excluded.entry_id,
                    invoice_number = excluded.invoice_number,
                    subject = excluded.subject,
                    received_at = excluded.received_at,
                    subject_po_number = excluded.subject_po_number,
                    pdf_po_number = excluded.pdf_po_number,
                    vin = excluded.vin,
                    invoice_amount = excluded.invoice_amount,
                    pdf_path = excluded.pdf_path,
                    updated_at = excluded.updated_at
                """,
                (
                    invoice.internet_message_id,
                    invoice.entry_id,
                    invoice.invoice_number,
                    invoice.subject,
                    invoice.received_at.isoformat(),
                    invoice.subject_po_number,
                    invoice.pdf_po_number,
                    invoice.vin,
                    str(invoice.invoice_amount),
                    invoice.pdf_path,
                    now,
                    now,
                ),
            )

    def block_duplicate_invoice_numbers(self) -> tuple[str, ...]:
        """Block every record sharing an invoice number in one transaction."""
        now = _utc_now()
        with self.connection() as connection, connection:
            rows = connection.execute(
                """
                SELECT invoice_number
                FROM invoices
                GROUP BY invoice_number
                HAVING COUNT(*) > 1
                ORDER BY invoice_number
                """
            ).fetchall()
            duplicate_numbers = tuple(str(row[0]) for row in rows)
            for invoice_number in duplicate_numbers:
                detail = f"Duplicate invoice number: {invoice_number}"
                connection.execute(
                    """
                    UPDATE invoices
                    SET identity_status = ?, last_error = ?, updated_at = ?
                    WHERE invoice_number = ?
                    """,
                    (StageStatus.BLOCKED, detail, now, invoice_number),
                )
            return duplicate_numbers

    def start_attempt(
        self,
        internet_message_id: str,
        run_id: str,
        stage: Stage,
    ) -> int:
        column = _STAGE_COLUMNS[stage]
        now = _utc_now()
        with self.connection() as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM invoices WHERE internet_message_id = ?",
                (internet_message_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"Unknown invoice message ID: {internet_message_id}")
            cursor = connection.execute(
                """
                INSERT INTO attempts (
                    internet_message_id, run_id, stage, status, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    internet_message_id,
                    run_id,
                    stage,
                    StageStatus.IN_PROGRESS,
                    now,
                ),
            )
            connection.execute(
                f"""
                UPDATE invoices
                SET {column} = ?, last_error = '', updated_at = ?
                WHERE internet_message_id = ?
                """,
                (StageStatus.IN_PROGRESS, now, internet_message_id),
            )
            return int(cursor.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        status: StageStatus,
        detail: str = "",
        *,
        mva: str | None = None,
        authorized_amount: str | None = None,
    ) -> None:
        if status in {StageStatus.PENDING, StageStatus.IN_PROGRESS}:
            raise ValueError(f"Attempt cannot finish with status {status}")
        now = _utc_now()
        with self.connection() as connection, connection:
            attempt = connection.execute(
                "SELECT internet_message_id, stage FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(f"Unknown attempt ID: {attempt_id}")
            stage = Stage(str(attempt["stage"]))
            column = _STAGE_COLUMNS[stage]
            connection.execute(
                """
                UPDATE attempts
                SET status = ?, detail = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (status, detail, now, attempt_id, StageStatus.IN_PROGRESS),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError(f"Attempt {attempt_id} is already complete")

            assignments = [f"{column} = ?", "last_error = ?", "updated_at = ?"]
            values: list[str] = [
                status,
                detail if status in {StageStatus.BLOCKED, StageStatus.FAILED} else "",
                now,
            ]
            if mva is not None:
                assignments.append("mva = ?")
                values.append(mva)
            if authorized_amount is not None:
                assignments.append("authorized_amount = ?")
                values.append(authorized_amount)
            values.append(str(attempt["internet_message_id"]))
            connection.execute(
                f"UPDATE invoices SET {', '.join(assignments)} "
                "WHERE internet_message_id = ?",
                values,
            )

    def get_invoice(self, internet_message_id: str) -> InvoiceState | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE internet_message_id = ?",
                (internet_message_id,),
            ).fetchone()
        return _row_to_state(row) if row is not None else None

    def get_invoice_readonly(
        self,
        internet_message_id: str,
    ) -> InvoiceState | None:
        if not self.database_path.is_file():
            return None
        database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM invoices WHERE internet_message_id = ?",
                (internet_message_id,),
            ).fetchone()
        return _row_to_state(row) if row is not None else None

    def find_by_vin(self, vin: str) -> tuple[InvoiceLookupRecord, ...]:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Invoice ledger not found: {self.database_path}")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT invoice_number, received_at, subject_po_number,
                       pdf_po_number, vin, mva, invoice_amount,
                       identity_status, outlook_status, last_error
                FROM invoices
                WHERE vin = ?
                ORDER BY received_at, invoice_number
                """,
                (vin,),
            ).fetchall()
        return tuple(
            InvoiceLookupRecord(
                invoice_number=str(row["invoice_number"]),
                received_at=str(row["received_at"]),
                subject_po_number=str(row["subject_po_number"]),
                pdf_po_number=str(row["pdf_po_number"]),
                vin=str(row["vin"]),
                mva=str(row["mva"]),
                invoice_amount=str(row["invoice_amount"]),
                identity_status=StageStatus(str(row["identity_status"])),
                outlook_status=StageStatus(str(row["outlook_status"])),
                last_error=str(row["last_error"]),
            )
            for row in rows
        )

    def get_review_history(self, internet_message_id: str) -> ReviewHistory | None:
        """Return failure evidence for one exact message without creating state."""
        if not self.database_path.is_file():
            return None
        with self.connection() as connection:
            attempt = connection.execute(
                """
                SELECT stage, status, detail
                FROM attempts
                WHERE internet_message_id = ? AND status IN (?, ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    internet_message_id,
                    StageStatus.BLOCKED,
                    StageStatus.FAILED,
                ),
            ).fetchone()
            if attempt is not None:
                return ReviewHistory(
                    stage=str(attempt["stage"]),
                    status=StageStatus(str(attempt["status"])),
                    detail=str(attempt["detail"]),
                )
            invoice = connection.execute(
                """
                SELECT identity_status, last_error
                FROM invoices
                WHERE internet_message_id = ?
                """,
                (internet_message_id,),
            ).fetchone()
        if invoice is None or not str(invoice["last_error"]):
            return None
        return ReviewHistory(
            stage=str(Stage.IDENTITY),
            status=StageStatus(str(invoice["identity_status"])),
            detail=str(invoice["last_error"]),
        )

    def record_review_event(
        self,
        *,
        invoice_number: str,
        internet_message_id: str,
        run_id: str,
        stage: str,
        code: str,
        detail: str,
        subject_po_number: str = "",
        pdf_po_number: str = "",
        vin: str = "",
        mva: str = "",
        fieldpo_mva: str = "",
    ) -> None:
        """Persist the reason after an invoice is successfully moved to review."""
        with self.connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO invoice_review_events (
                    invoice_number, internet_message_id, run_id, stage,
                    code, detail, subject_po_number, pdf_po_number, vin, mva,
                    fieldpo_mva, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_number,
                    internet_message_id,
                    run_id,
                    stage,
                    code,
                    detail,
                    subject_po_number,
                    pdf_po_number,
                    vin,
                    mva,
                    fieldpo_mva,
                    _utc_now(),
                ),
            )

    def find_review_records(
        self,
        identifier: str,
    ) -> tuple[InvoiceReviewLookup, ...]:
        """Find review reasons by exact MVA, VIN, or PO."""
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Invoice ledger not found: {self.database_path}")

        identifier = identifier.strip()
        database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            has_review_events = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'invoice_review_events'"
            ).fetchone() is not None
            event_columns = (
                """
                    review_event.stage AS event_stage,
                    review_event.code AS event_code,
                    review_event.detail AS event_detail,
                    review_event.invoice_number AS event_invoice_number,
                    review_event.subject_po_number AS event_subject_po_number,
                    review_event.pdf_po_number AS event_pdf_po_number,
                    review_event.vin AS event_vin,
                    review_event.mva AS event_mva,
                    review_event.fieldpo_mva AS event_fieldpo_mva,
                """
                if has_review_events
                else """
                    NULL AS event_stage,
                    NULL AS event_code,
                    NULL AS event_detail,
                    NULL AS event_invoice_number,
                    NULL AS event_subject_po_number,
                    NULL AS event_pdf_po_number,
                    NULL AS event_vin,
                    NULL AS event_mva,
                    NULL AS event_fieldpo_mva,
                """
            )
            event_join = (
                """
                LEFT JOIN invoice_review_events AS review_event
                    ON review_event.id = (
                        SELECT id
                        FROM invoice_review_events
                        WHERE internet_message_id = invoice.internet_message_id
                        ORDER BY id
                        LIMIT 1
                    )
                """
                if has_review_events
                else ""
            )
            rows = connection.execute(
                f"""
                SELECT
                    invoice.internet_message_id,
                    invoice.received_at,
                    invoice.invoice_number,
                    invoice.subject_po_number,
                    invoice.pdf_po_number,
                    invoice.vin,
                    invoice.mva,
                    invoice.invoice_amount,
                    invoice.identity_status,
                    invoice.outlook_status,
                    invoice.last_error,
                    {event_columns}
                    attempt.stage AS review_stage,
                    attempt.status AS review_status,
                    attempt.detail AS review_detail
                FROM invoices AS invoice
                {event_join}
                LEFT JOIN attempts AS attempt
                    ON attempt.id = (
                        SELECT id
                        FROM attempts
                        WHERE internet_message_id = invoice.internet_message_id
                            AND status IN (?, ?)
                        ORDER BY id
                        LIMIT 1
                    )
                ORDER BY invoice.received_at, invoice.internet_message_id
                """,
                (StageStatus.BLOCKED, StageStatus.FAILED),
            ).fetchall()
            unmatched_events = ()
            if has_review_events:
                unmatched_events = connection.execute(
                    """
                    SELECT
                        invoice_number,
                        stage,
                        code,
                        detail,
                        subject_po_number,
                        pdf_po_number,
                        vin,
                        mva,
                        fieldpo_mva
                    FROM invoice_review_events AS review_event
                    WHERE NOT EXISTS (
                            SELECT 1
                            FROM invoices AS invoice
                            WHERE invoice.internet_message_id =
                                review_event.internet_message_id
                        )
                        AND review_event.id = (
                            SELECT MIN(id)
                            FROM invoice_review_events
                            WHERE invoice_number = review_event.invoice_number
                                AND internet_message_id =
                                    review_event.internet_message_id
                        )
                    ORDER BY review_event.id
                    """,
                    (),
                ).fetchall()

        records = []
        for row in rows:
            review_history = None
            if row["event_stage"] is not None:
                review_history = ReviewHistory(
                    stage=str(row["event_stage"]),
                    status=StageStatus.BLOCKED,
                    detail=str(row["event_detail"]),
                    code=str(row["event_code"]),
                )
            elif row["review_stage"] is not None:
                review_history = ReviewHistory(
                    stage=str(row["review_stage"]),
                    status=StageStatus(str(row["review_status"])),
                    detail=str(row["review_detail"]),
                )
            elif str(row["last_error"]):
                review_history = ReviewHistory(
                    stage=str(Stage.IDENTITY),
                    status=StageStatus(str(row["identity_status"])),
                    detail=str(row["last_error"]),
                )
            reason_detail = (
                review_history.detail if review_history is not None else ""
            )
            matched_by = _matched_identifier_kinds(
                identifier,
                subject_po_number=str(row["subject_po_number"]),
                pdf_po_number=str(row["pdf_po_number"]),
                vin=str(row["vin"]),
                mvas=(
                    str(row["mva"]),
                    str(row["event_mva"] or ""),
                    str(row["event_fieldpo_mva"] or ""),
                ),
                reason_detail=reason_detail,
            )
            if not matched_by:
                continue
            records.append(
                InvoiceReviewLookup(
                    invoice_number=str(row["invoice_number"]),
                    matched_by=matched_by,
                    received_at=str(row["received_at"]),
                    pdf_po_number=str(row["pdf_po_number"]),
                    vin=str(row["vin"]),
                    mva=str(row["mva"]),
                    invoice_amount=str(row["invoice_amount"]),
                    identity_status=StageStatus(str(row["identity_status"])),
                    outlook_status=StageStatus(str(row["outlook_status"])),
                    review_history=review_history,
                )
            )
        for row in unmatched_events:
            matched_by = _matched_identifier_kinds(
                identifier,
                subject_po_number=str(row["subject_po_number"]),
                pdf_po_number=str(row["pdf_po_number"]),
                vin=str(row["vin"]),
                mvas=(str(row["mva"]), str(row["fieldpo_mva"])),
                reason_detail=str(row["detail"]),
            )
            if not matched_by:
                continue
            records.append(
                InvoiceReviewLookup(
                    invoice_number=str(row["invoice_number"]),
                    matched_by=matched_by,
                    received_at="",
                    pdf_po_number="",
                    vin="",
                    mva="",
                    invoice_amount="",
                    identity_status=StageStatus.BLOCKED,
                    outlook_status=StageStatus.PENDING,
                    review_history=ReviewHistory(
                        stage=str(row["stage"]),
                        status=StageStatus.BLOCKED,
                        detail=str(row["detail"]),
                        code=str(row["code"]),
                    ),
                )
            )
        return tuple(records)

    def list_terminal_identity_message_ids(self) -> tuple[str, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT internet_message_id
                FROM invoices
                WHERE identity_status IN (?, ?)
                ORDER BY received_at, internet_message_id
                """,
                (StageStatus.COMPLETE, StageStatus.BLOCKED),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_attempts(self, internet_message_id: str) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, stage, status, detail, started_at, completed_at
                FROM attempts
                WHERE internet_message_id = ?
                ORDER BY id
                """,
                (internet_message_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def _row_to_state(row: sqlite3.Row) -> InvoiceState:
    return InvoiceState(
        internet_message_id=str(row["internet_message_id"]),
        invoice_number=str(row["invoice_number"]),
        subject_po_number=str(row["subject_po_number"]),
        pdf_po_number=str(row["pdf_po_number"]),
        vin=str(row["vin"]),
        mva=str(row["mva"]),
        invoice_amount=str(row["invoice_amount"]),
        authorized_amount=str(row["authorized_amount"]),
        identity_status=StageStatus(str(row["identity_status"])),
        compass_status=StageStatus(str(row["compass_status"])),
        po_status=StageStatus(str(row["po_status"])),
        work_order_status=StageStatus(str(row["work_order_status"])),
        outlook_status=StageStatus(str(row["outlook_status"])),
        last_error=str(row["last_error"]),
    )


def _matched_identifier_kinds(
    identifier: str,
    *,
    subject_po_number: str,
    pdf_po_number: str,
    vin: str,
    mvas: tuple[str, ...],
    reason_detail: str,
) -> tuple[str, ...]:
    normalized = identifier.strip().upper()
    matches = []
    if normalized in {subject_po_number.upper(), pdf_po_number.upper()}:
        matches.append("PO")
    if normalized == vin.upper():
        matches.append("VIN")

    identifier_mvas = {identifier}
    is_mva = (
        identifier.isascii()
        and identifier.isdigit()
        and len(identifier) in {8, 9}
    )
    if is_mva:
        identifier_mvas.add(identifier.zfill(9))
    recorded_mvas = {
        value.upper()
        for mva in mvas
        for value in re.split(r"[^A-Za-z0-9]+", mva)
        if value
    }
    if identifier_mvas.intersection(recorded_mvas):
        matches.append("MVA")
    elif is_mva and "MVA" in reason_detail.upper():
        reason_values = set(
            re.findall(r"(?<![A-Z0-9])\d{8,9}(?![A-Z0-9])", reason_detail.upper())
        )
        if identifier_mvas.intersection(reason_values):
            matches.append("MVA")
    return tuple(matches)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
