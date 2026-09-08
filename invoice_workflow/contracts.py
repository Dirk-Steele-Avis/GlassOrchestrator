"""Typed contracts shared by the completed-invoice workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Sequence


class Stage(StrEnum):
    IDENTITY = "identity"
    COMPASS = "compass"
    FIELDPO_PO = "fieldpo_po"
    FIELDPO_WORK_ORDER = "fieldpo_work_order"
    OUTLOOK = "outlook"


class StageStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ParsedInvoice:
    internet_message_id: str
    entry_id: str
    invoice_number: str
    subject: str
    received_at: datetime
    subject_po_number: str
    pdf_po_number: str
    vin: str
    invoice_amount: Decimal
    pdf_path: str


@dataclass(frozen=True, slots=True)
class CaptureFailure:
    entry_id: str
    subject: str
    detail: str
    internet_message_id: str = ""


@dataclass(frozen=True, slots=True)
class OutlookScanResult:
    invoices: tuple[ParsedInvoice, ...]
    failures: tuple[CaptureFailure, ...]


@dataclass(frozen=True, slots=True)
class ValidatedIdentity:
    internet_message_id: str
    invoice_number: str
    po_number: str
    vin: str
    mva: str
    invoice_amount: Decimal
    authorized_amount: Decimal


@dataclass(frozen=True, slots=True)
class IdentityResult:
    status: StageStatus
    identity: ValidatedIdentity | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FieldPOIdentitySnapshot:
    po_number: str
    vin: str
    mva: str
    authorized_amount: Decimal


@dataclass(frozen=True, slots=True)
class CompassTarget:
    internet_message_id: str
    invoice_number: str
    mva: str
    complaint_type: str = "Glass"


@dataclass(frozen=True, slots=True)
class CompassResult:
    internet_message_id: str
    status: StageStatus
    detail: str = ""
    duration_seconds: float = 0.0
    screenshot_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldPOTarget:
    internet_message_id: str
    invoice_number: str
    po_number: str
    vin: str
    mva: str
    invoice_amount: Decimal


@dataclass(frozen=True, slots=True)
class FieldPOResult:
    internet_message_id: str
    po_status: StageStatus
    work_order_status: StageStatus
    detail: str = ""
    duration_seconds: float = 0.0
    screenshot_paths: tuple[str, ...] = ()


class OutlookSource(Protocol):
    def scan_parent_invoices(
        self,
        *,
        invoice_number: str | None = None,
        max_invoices: int | None = None,
        excluded_message_ids: frozenset[str] = frozenset(),
    ) -> OutlookScanResult: ...

    def move_to_processed(self, internet_message_id: str) -> None: ...

    def move_to_needs_review(self, internet_message_id: str) -> None: ...


class IdentityResolver(Protocol):
    def resolve(self, invoice: ParsedInvoice) -> IdentityResult: ...


class SheetIdentityReader(Protocol):
    def find_mvas_by_vin(self, vin: str) -> Sequence[str]: ...


class FieldPOIdentityReader(Protocol):
    def read_identity(self, po_number: str) -> FieldPOIdentitySnapshot: ...


class CompassWorker(Protocol):
    async def run(self, targets: Sequence[CompassTarget]) -> Sequence[CompassResult]: ...


class FieldPOWorker(Protocol):
    def run(self, targets: Sequence[FieldPOTarget]) -> Sequence[FieldPOResult]: ...
