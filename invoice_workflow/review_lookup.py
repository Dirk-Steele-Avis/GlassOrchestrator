"""Read-only lookup of recorded invoice review reasons."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from invoice_workflow.store import InvoiceRepository

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT / "data" / "completed_invoices.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show recorded review reasons using an MVA, VIN, or PO."
    )
    identifiers = parser.add_mutually_exclusive_group(required=True)
    identifiers.add_argument("--mva", type=_mva)
    identifiers.add_argument("--vin", type=_vin)
    identifiers.add_argument("--po", type=_po)
    args = parser.parse_args(argv)
    if args.mva is not None:
        identifier, identifier_type = args.mva, "MVA"
    elif args.vin is not None:
        identifier, identifier_type = args.vin, "VIN"
    elif args.po is not None:
        identifier, identifier_type = args.po, "PO"
    else:
        parser.error("provide an MVA, VIN, or PO")

    repository = InvoiceRepository(DATABASE_PATH)
    try:
        records = repository.find_review_records(identifier)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not records:
        print(
            f"No ledger records found for {identifier_type} {identifier}. "
            "Try a different MVA, VIN, or PO if available."
        )
        return 1

    print(f"{identifier_type} {identifier}: {len(records)} matching record(s)")
    for index, record in enumerate(records, start=1):
        if index > 1:
            print()
        print(f"Record {index}")
        print(f"  Invoice number: {record.invoice_number or '(not recorded)'}")
        print(f"  Matched by: {', '.join(record.matched_by)}")
        print(f"  Received: {record.received_at or '(not recorded)'}")
        print(f"  PO: {record.pdf_po_number or '(not recorded)'}")
        print(f"  VIN: {record.vin or '(not recorded)'}")
        print(f"  MVA: {record.mva or '(not recorded)'}")
        print(f"  Invoice amount: {record.invoice_amount or '(not recorded)'}")
        print(f"  Identity status: {record.identity_status}")
        print(f"  Outlook status: {record.outlook_status}")
        if record.review_history is None:
            print("  Review reason: No blocked/failed reason is recorded.")
        else:
            print(
                f"  Review stage/status: {record.review_history.stage}/"
                f"{record.review_history.status}"
            )
            if record.review_history.code:
                print(f"  Review code: {record.review_history.code}")
            print(f"  Review reason: {record.review_history.detail}")
    return 0


def _mva(value: str) -> str:
    normalized = value.strip().upper()
    if re.fullmatch(r"\d{8,9}", normalized) is None:
        raise argparse.ArgumentTypeError("MVA must contain exactly 8 or 9 digits")
    return normalized


def _vin(value: str) -> str:
    normalized = value.strip().upper()
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", normalized) is None:
        raise argparse.ArgumentTypeError(
            "VIN must contain exactly 17 valid VIN characters"
        )
    return normalized


def _po(value: str) -> str:
    normalized = value.strip().upper()
    if re.fullmatch(r"FPO\d{7,}", normalized) is None:
        raise argparse.ArgumentTypeError("PO must be an FPO number")
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
