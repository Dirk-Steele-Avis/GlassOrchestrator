"""SQLite repository for durable completed-invoice workflow state."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from invoice_workflow.contracts import ParsedInvoice, Stage, StageStatus

SCHEMA_VERSION = 1
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
