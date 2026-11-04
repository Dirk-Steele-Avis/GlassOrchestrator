"""Read-only lookup of captured completed invoices by VIN or MVA."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Protocol

from invoice_workflow.identity_adapters import GoogleSheetIdentityReader
from invoice_workflow.store import InvoiceLookupRecord, InvoiceRepository

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT / "data" / "completed_invoices.db"


class MvaResolver(Protocol):
    def find_vins_by_mva(self, mva: str) -> tuple[str, ...]: ...


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repository = InvoiceRepository(DATABASE_PATH)
    resolver = _build_mva_resolver() if args.mvas else None

    for vin in args.vins:
        _print_results(
            "VIN", vin, vin, repository.find_by_vin(vin), args.received_only
        )

    for mva in args.mvas:
        assert resolver is not None
        vins = resolver.find_vins_by_mva(mva)
        if len(vins) != 1:
            raise RuntimeError(
                f"Expected exactly one GlassClaims VIN for MVA {mva}, found {len(vins)}"
            )
        _print_results(
            "MVA", mva, vins[0], repository.find_by_vin(vins[0]), args.received_only
        )
    return 0


def _print_results(
    target_type: str,
    target: str,
    vin: str,
    records: tuple[InvoiceLookupRecord, ...],
    received_only: bool = False,
) -> None:
    target_label = f"{target_type} {target}"
    vin_label = f" | VIN {vin}" if target_type == "MVA" else ""
    if not records:
        if received_only:
            return
        print(f"{target_label}{vin_label} | NOT FOUND IN INVOICE LEDGER")
        return
    for record in records:
        po_number = record.pdf_po_number or record.subject_po_number
        result_label = (
            target_label
            if target_type == "VIN"
            else f"{target_label} | VIN {record.vin}"
        )
        print(
            f"{result_label} | RECEIVED | "
            f"invoice={record.invoice_number} | po={po_number} | "
            f"amount=${record.invoice_amount} | received={record.received_at} | "
            f"identity={record.identity_status} | outlook={record.outlook_status}"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the completed-invoice ledger by VIN or MVA without changing workflow state"
    )
    parser.add_argument("--vins", action="append", default=[])
    parser.add_argument("--mvas", action="append", default=[])
    parser.add_argument(
        "--received-only",
        action="store_true",
        help="Show only VINs/MVAs with an invoice in the completed-invoice ledger.",
    )
    args = parser.parse_args(argv)
    try:
        args.vins = _parse_values(args.vins, r"[A-HJ-NPR-Z0-9]{17}", "VIN")
        args.mvas = tuple(
            value.zfill(9)
            for value in _parse_values(args.mvas, r"\d{8,9}", "MVA")
        )
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if not args.vins and not args.mvas:
        parser.error("provide --vins and/or --mvas")
    return args


def _parse_values(raw_values: list[str], pattern: str, label: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for token in re.split(r"[\s,;]+", raw_value.strip().upper()):
            if not token:
                continue
            if re.fullmatch(pattern, token) is None:
                raise argparse.ArgumentTypeError(f"invalid {label}: {token}")
            if token not in seen:
                seen.add(token)
                values.append(token)
    return tuple(values)


def _build_mva_resolver() -> GoogleSheetIdentityReader:
    config = _load_runtime_config()
    service_account_path = Path(_required_text(config, "service_account_json"))
    if not service_account_path.is_absolute():
        service_account_path = ROOT / service_account_path
    return GoogleSheetIdentityReader(
        spreadsheet_id=_required_text(config, "spreadsheet_id"),
        sheet_name=_required_text(config, "sheet_name"),
        service_account_json=service_account_path,
    )


def _load_runtime_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in (
        ROOT / "orchestrator_config.json",
        ROOT / "orchestrator_project.json",
        ROOT / "orchestrator_project.local.json",
        ROOT / "outlook" / "agn_invoices_config.json",
    ):
        if path.exists():
            with path.open("r", encoding="utf-8") as config_file:
                payload = json.load(config_file)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in {path}")
            config.update(payload)
    return config


def _required_text(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key, "")).strip()
    if not value or value == "YOUR_SPREADSHEET_ID_HERE":
        raise ValueError(f"Missing completed-invoice config value: {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())