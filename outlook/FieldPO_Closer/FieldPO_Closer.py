"""Close exact FieldPO POs from an explicit VIN or PO queue."""

from __future__ import annotations

import argparse
import csv
from functools import partial
import json
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

import pythoncom
import win32com.client
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from invoice_workflow.outlook_source import OutlookInvoiceSource
from invoice_workflow.identity_adapters import FieldPOBrowserIdentityReader
from invoice_workflow.invoice_parser import parse_invoice_pdf
from outlook import agn_invoices

TOOL_DIR = Path(__file__).resolve().parent
INPUT_FILES = {
    "vin": TOOL_DIR / "vins.csv",
    "po": TOOL_DIR / "pos.csv",
}
ATTACHMENT_DIR = TOOL_DIR / "attachments"
CONFIG_PATH = ROOT / "outlook" / "agn_invoices_config.json"
PROCESSABLE_RESULTS = {"", "missing invoice"}
VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
PO_PATTERN = re.compile(r"FPO\d{7,}", re.IGNORECASE)


@dataclass(slots=True)
class VinRow:
    vin: str
    result: str


@dataclass(slots=True)
class PoRow:
    po_number: str
    result: str
    vin: str = ""


def load_rows(mode: str, path: Path | None = None) -> list[VinRow] | list[PoRow]:
    if mode not in INPUT_FILES:
        raise ValueError(f"Unsupported FieldPO closer mode: {mode}")
    csv_path = path or INPUT_FILES[mode]
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if mode == "vin":
            if reader.fieldnames != ["VIN", "Result"]:
                raise ValueError("vins.csv must contain exactly these headers: VIN,Result")
            return [
                VinRow(
                    str(row.get("VIN") or "").strip().upper(),
                    str(row.get("Result") or "").strip(),
                )
                for row in reader
            ]
        if reader.fieldnames != ["PO", "VIN", "Result"]:
            raise ValueError("pos.csv must contain exactly these headers: PO,VIN,Result")
        return [
            PoRow(
                str(row.get("PO") or "").strip().upper(),
                str(row.get("Result") or "").strip(),
                str(row.get("VIN") or "").strip().upper(),
            )
            for row in reader
        ]


def save_rows(
    mode: str,
    rows: list[VinRow] | list[PoRow],
    path: Path | None = None,
) -> None:
    if mode not in INPUT_FILES:
        raise ValueError(f"Unsupported FieldPO closer mode: {mode}")
    csv_path = path or INPUT_FILES[mode]
    temporary_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        if mode == "vin":
            writer.writerow(["VIN", "Result"])
            writer.writerows((row.vin, row.result) for row in rows)
        else:
            writer.writerow(["PO", "VIN", "Result"])
            writer.writerows((row.po_number, row.vin, row.result) for row in rows)
    temporary_path.replace(csv_path)


def process_rows(
    mode: str,
    rows: list[VinRow] | list[PoRow],
    invoices: list[Any],
    process_invoices: Callable[[list[Any]], str | Mapping[str, str]],
    save: Callable[[list[VinRow] | list[PoRow]], None],
    resolve_fieldpo_vin: Callable[[str], str] | None = None,
    move_to_review: Callable[[str], None] | None = None,
    move_to_processed: Callable[[str], None] | None = None,
) -> None:
    if mode == "vin":
        _process_vin_rows(
            rows, invoices, process_invoices, save, move_to_review, move_to_processed
        )
    elif mode == "po":
        _process_po_rows(
            rows,
            invoices,
            process_invoices,
            save,
            resolve_fieldpo_vin,
            move_to_review,
            move_to_processed,
        )
    else:
        raise ValueError(f"Unsupported FieldPO closer mode: {mode}")


def _process_vin_rows(
    rows: list[VinRow] | list[PoRow],
    invoices: list[Any],
    process_invoices: Callable[[list[Any]], str | Mapping[str, str]],
    save: Callable[[list[VinRow] | list[PoRow]], None],
    move_to_review: Callable[[str], None] | None,
    move_to_processed: Callable[[str], None] | None,
) -> None:
    if any(not isinstance(row, VinRow) for row in rows):
        raise TypeError("VIN mode requires VIN queue rows")
    eligible_by_vin: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        eligible_by_vin.setdefault(row.vin.strip().upper(), []).append(index)
    invoices_by_vin: dict[str, list[Any]] = {}
    for invoice in invoices:
        invoices_by_vin.setdefault(str(invoice.vin).strip().upper(), []).append(invoice)

    result_counts: dict[str, int] = {}
    for position, (vin, indices) in enumerate(eligible_by_vin.items(), start=1):
        started_at = time.perf_counter()
        matches = invoices_by_vin.get(vin.strip().upper(), [])
        print(
            f"[{position}/{len(eligible_by_vin)}] Processing VIN "
            f"{vin or '<blank VIN>'}...",
            flush=True,
        )
        if VIN_PATTERN.fullmatch(vin) is None:
            result = "Review"
        else:
            if not matches:
                result = "Missing Invoice"
            else:
                try:
                    outcomes = process_invoices(matches)
                except Exception as exc:
                    result = "Review"
                    _move_invoices_to_needs_review(matches, move_to_review)
                else:
                    if isinstance(outcomes, Mapping):
                        matched_ids = {
                            str(invoice.internet_message_id) for invoice in matches
                        }
                        if set(outcomes) != matched_ids:
                            raise ValueError(
                                "Invoice outcome IDs do not match the scanned VIN invoices"
                            )
                        for invoice in matches:
                            message_id = str(invoice.internet_message_id)
                            if outcomes[message_id] == "Success":
                                if move_to_processed is not None:
                                    move_to_processed(message_id)
                            elif outcomes[message_id] == "Review":
                                _move_message_to_needs_review(message_id, move_to_review)
                            elif outcomes[message_id] == "Unverified":
                                continue
                            else:
                                raise ValueError(
                                    f"Unsupported invoice outcome for {message_id}: "
                                    f"{outcomes[message_id]}"
                                )
                        result = (
                            "Success"
                            if all(outcome == "Success" for outcome in outcomes.values())
                            else "Review"
                        )
                    else:
                        result = outcomes
                        if result == "Review":
                            _move_invoices_to_needs_review(matches, move_to_review)
        for index in indices:
            rows[index].result = result
        save(rows)
        result_counts[result] = result_counts.get(result, 0) + 1
        print(
            f"[{position}/{len(eligible_by_vin)}] VIN {vin or '<blank VIN>'}: "
            f"{result} ({time.perf_counter() - started_at:.1f}s)",
            flush=True,
        )
    summary = ", ".join(
        f"{status}={count}" for status, count in sorted(result_counts.items())
    )
    print(f"VIN results: {summary or 'none'}", flush=True)


def _process_po_rows(
    rows: list[VinRow] | list[PoRow],
    invoices: list[Any],
    process_invoices: Callable[[list[Any]], str],
    save: Callable[[list[VinRow] | list[PoRow]], None],
    resolve_fieldpo_vin: Callable[[str], str] | None,
    move_to_review: Callable[[str], None] | None,
    move_to_processed: Callable[[str], None] | None,
) -> None:
    if any(not isinstance(row, PoRow) for row in rows):
        raise TypeError("PO mode requires PO queue rows")
    eligible_by_po: dict[str, list[int]] = {}
    indices_by_po: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        po_number = row.po_number.strip().upper()
        indices_by_po.setdefault(po_number, []).append(index)
        if row.result.strip().casefold() in PROCESSABLE_RESULTS:
            eligible_by_po.setdefault(po_number, []).append(index)
    invoices_by_po: dict[str, list[Any]] = {}
    for invoice in invoices:
        invoice_pos = {
            str(invoice.subject_po_number).strip().upper(),
            str(invoice.pdf_po_number).strip().upper(),
        }
        for po_number in invoice_pos:
            if PO_PATTERN.fullmatch(po_number):
                invoices_by_po.setdefault(po_number, []).append(invoice)

    results: dict[str, tuple[str, str]] = {}
    invoices_by_vin: dict[str, list[Any]] = {}
    pos_by_vin: dict[str, list[str]] = {}
    fieldpo_vins: dict[str, str] = {}
    vin_changed = False
    for po_number, row_indices in indices_by_po.items():
        if po_number not in eligible_by_po and all(
            rows[index].vin.strip() for index in row_indices
        ):
            continue
        if PO_PATTERN.fullmatch(po_number) is None:
            if po_number in eligible_by_po:
                results[po_number] = ("Review", "invalid PO number")
            continue
        if resolve_fieldpo_vin is None:
            if po_number in eligible_by_po:
                results[po_number] = (
                    "Review",
                    "exact FieldPO VIN resolver is unavailable",
                )
            continue
        try:
            fieldpo_vin = str(resolve_fieldpo_vin(po_number)).strip().upper()
        except Exception as exc:
            detail = f"FieldPO PO lookup failed: {type(exc).__name__}: {exc}"
            if po_number in eligible_by_po:
                results[po_number] = ("Review", detail)
            else:
                print(f"{po_number}: VIN lookup failed ({detail})", flush=True)
            continue
        if VIN_PATTERN.fullmatch(fieldpo_vin) is None:
            if po_number in eligible_by_po:
                results[po_number] = ("Review", "FieldPO returned an invalid VIN")
            else:
                print(f"{po_number}: FieldPO returned an invalid VIN", flush=True)
            continue
        fieldpo_vins[po_number] = fieldpo_vin
        for index in row_indices:
            if rows[index].vin != fieldpo_vin:
                rows[index].vin = fieldpo_vin
                vin_changed = True
        if po_number not in eligible_by_po:
            result = rows[row_indices[0]].result
            print(
                f"{po_number} VIN {fieldpo_vin}: {result} (VIN lookup only)",
                flush=True,
            )

    if vin_changed:
        save(rows)

    for po_number in eligible_by_po:
        if po_number in results:
            continue
        matches = invoices_by_po.get(po_number, [])
        if not matches:
            results[po_number] = (
                "Missing Invoice",
                "no matching invoice in Invoice, Processed, or Needs Review",
            )
            continue
        if len(matches) != 1:
            results[po_number] = ("Review", "multiple matching invoices")
            continue
        invoice = matches[0]
        subject_po = str(invoice.subject_po_number).strip().upper()
        pdf_po = str(invoice.pdf_po_number).strip().upper()
        if (subject_po and subject_po != po_number) or pdf_po != po_number:
            results[po_number] = (
                "Review",
                "requested PO does not match both invoice subject and PDF",
            )
            continue
        fieldpo_vin = fieldpo_vins.get(po_number, "")
        if not fieldpo_vin:
            results[po_number] = ("Review", "FieldPO VIN was not resolved")
            continue
        invoice_vin = str(invoice.vin or "").strip().upper()
        if invoice_vin and invoice_vin != fieldpo_vin:
            results[po_number] = ("Review", "invoice VIN does not match FieldPO VIN")
            continue
        invoices_by_vin.setdefault(fieldpo_vin, []).append(invoice)
        pos_by_vin.setdefault(fieldpo_vin, []).append(po_number)

    for vin, matching_invoices in invoices_by_vin.items():
        matching_pos = pos_by_vin[vin]
        try:
            outcome = process_invoices(matching_invoices)
            if isinstance(outcome, Mapping):
                result = (
                    "Success"
                    if all(item == "Success" for item in outcome.values())
                    else "Review"
                )
            else:
                result = outcome
            detail = (
                "FieldPO checks passed"
                if result == "Success"
                else "a FieldPO gate requires review"
            )
        except Exception as exc:
            result = "Review"
            detail = f"{type(exc).__name__}: {exc}"
        for po_number in matching_pos:
            results[po_number] = (result, detail)

    for position, (po_number, indices) in enumerate(eligible_by_po.items(), start=1):
        result, detail = results[po_number]
        if result == "Review":
            _move_invoices_to_needs_review(
                invoices_by_po.get(po_number, []), move_to_review
            )
        elif result == "Success" and move_to_processed is not None:
            for invoice in invoices_by_po.get(po_number, []):
                move_to_processed(str(invoice.internet_message_id))
        for index in indices:
            rows[index].result = result
        save(rows)
        vin = rows[indices[0]].vin or "<not resolved>"
        print(
            f"[{position}/{len(eligible_by_po)}] {po_number or '<blank PO>'} "
            f"VIN {vin}: "
            f"{result} ({detail}; rows={len(indices)})",
            flush=True,
        )


def _move_invoices_to_needs_review(
    invoices: list[Any],
    move_to_review: Callable[[str], None] | None,
) -> None:
    if move_to_review is None:
        return
    for invoice in invoices:
        message_id = str(invoice.internet_message_id).strip()
        _move_message_to_needs_review(message_id, move_to_review)


def _move_message_to_needs_review(
    message_id: str,
    move_to_review: Callable[[str], None] | None,
) -> None:
    if not message_id:
        raise ValueError("Cannot move an invoice without its InternetMessageID")
    if move_to_review is not None:
        move_to_review(message_id)


def process_fieldpo_invoices(
    page: Any,
    invoices: list[Any],
    *,
    fieldpo_vins: dict[str, str] | None = None,
) -> dict[str, str]:
    invoices_by_po: dict[str, list[Any]] = {}
    for invoice in invoices:
        po_number = str(invoice.pdf_po_number).strip().upper()
        if PO_PATTERN.fullmatch(po_number) is None:
            _review_invoice(invoice, "PDF PO number is missing or invalid")
            continue
        invoices_by_po.setdefault(po_number, []).append(invoice)

    outcomes: dict[str, str] = {}
    for po_number, candidates in invoices_by_po.items():
        candidates.sort(key=lambda invoice: invoice.received_at)
        terminal_invoice = None
        terminal_outcome = ""
        for invoice in candidates:
            message_id = str(invoice.internet_message_id)
            if (
                (
                    str(invoice.subject_po_number).strip().upper()
                    and str(invoice.subject_po_number).strip().upper() != po_number
                )
                or str(invoice.pdf_po_number).strip().upper() != po_number
                or _invoice_total(invoice) is None
            ):
                _review_invoice(invoice, "invoice PO or total is invalid")
                outcomes[message_id] = "Review"
                continue
            if terminal_invoice is not None:
                outcomes[message_id] = (
                    terminal_outcome
                    if _same_validated_invoice(invoice, terminal_invoice)
                    else "Review"
                )
                continue
            fieldpo_vin = (
                fieldpo_vins.get(po_number, "") if fieldpo_vins is not None else ""
            )
            result = process_fieldpo_invoice(
                page,
                invoice,
                close_work_order=True,
                fieldpo_vin=fieldpo_vin or None,
            )
            outcomes[message_id] = result
            if result in {"Success", "Unverified"}:
                terminal_invoice = invoice
                terminal_outcome = result

    for invoice in invoices:
        outcomes.setdefault(str(invoice.internet_message_id), "Review")
    return outcomes


def _invoice_total(invoice: Any) -> Decimal | None:
    total = getattr(invoice, "invoice_total", None)
    return total if isinstance(total, Decimal) else None


def _same_validated_invoice(invoice: Any, validated_invoice: Any) -> bool:
    return (
        str(invoice.invoice_number).strip()
        == str(validated_invoice.invoice_number).strip()
        and str(invoice.pdf_po_number).strip().upper()
        == str(validated_invoice.pdf_po_number).strip().upper()
        and str(invoice.vin).strip().upper()
        == str(validated_invoice.vin).strip().upper()
        and _invoice_total(invoice) == _invoice_total(validated_invoice)
    )


def _review_invoice(invoice: Any, reason: str) -> str:
    return "Review"


def process_fieldpo_invoice(
    page: Any,
    invoice: Any,
    *,
    close_work_order: bool = True,
    fieldpo_vin: str | None = None,
) -> str:
    subject_po = str(invoice.subject_po_number).strip().upper()
    pdf_po = str(invoice.pdf_po_number).strip().upper()
    if not pdf_po or (subject_po and subject_po != pdf_po):
        return _review_invoice(invoice, "email subject PO does not match PDF PO")

    target_vin = str(fieldpo_vin or invoice.vin or "").strip().upper()
    if VIN_PATTERN.fullmatch(target_vin) is None:
        return _review_invoice(invoice, "invoice or FieldPO VIN is invalid")
    invoice_vin = str(invoice.vin or "").strip().upper()
    if (not invoice_vin and fieldpo_vin is None) or (
        invoice_vin and invoice_vin != target_vin
    ):
        return _review_invoice(invoice, "invoice VIN does not match FieldPO VIN")

    found_mva = agn_invoices.go_to_active_work_order_tab(page, target_vin)
    if not found_mva or not agn_invoices.po_exists(page, pdf_po):
        return _review_invoice(invoice, "VIN or exact PO was not found on the active work order")

    listed_pos = _listed_po_numbers(page)
    if pdf_po not in listed_pos:
        return _review_invoice(invoice, "exact PDF PO was not found on the work order")

    created_by = agn_invoices.read_work_order_created_by(page)
    if created_by.strip().casefold() != "steele, dirk":
        return _review_invoice(
            invoice,
            f"work-order creator is {created_by or 'missing'}, not Steele, Dirk",
        )

    work_order_completed = _work_order_is_completed(page)
    agn_invoices.click_into_po(page, pdf_po)
    status, authorized_amount = agn_invoices.read_po_status_and_amount(page)
    invoice_total = _invoice_total(invoice)
    if invoice_total is None:
        return _review_invoice(invoice, "PDF total is missing or invalid")
    if _amount(authorized_amount) != invoice_total:
        return _review_invoice(
            invoice,
            f"PDF total ${invoice_total} does not match FieldPO authorized amount "
            f"${_amount(authorized_amount)}",
        )
    if status == "APPROVED":
        if work_order_completed:
            return "Success"
        return "Unverified"
    if status != "IN PROGRESS":
        return _review_invoice(invoice, f"FieldPO PO status is {status}")

    closed = agn_invoices.approve_displayed_po(
        page,
        target_vin,
        request_permission=False,
        expected_po_number=pdf_po,
        close_work_order=close_work_order,
        allow_open_if_closure_unavailable=True,
    )
    return "Success"


def _work_order_is_completed(page: Any) -> bool:
    completed_badge_count = page.get_by_text("COMPLETED", exact=True).count()
    if completed_badge_count > 1:
        raise RuntimeError(
            "Expected at most one exact FieldPO COMPLETED work-order status, "
            f"found {completed_badge_count}"
        )
    return completed_badge_count == 1


def _listed_po_numbers(page: Any) -> set[str]:
    texts = page.get_by_text(PO_PATTERN, exact=True).all_inner_texts()
    return {text.strip().upper() for text in texts if PO_PATTERN.fullmatch(text.strip())}


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def _load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        payload = json.load(config_file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {CONFIG_PATH}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Close FieldPOs against Outlook invoices.")
    parser.add_argument("--mode", choices=tuple(INPUT_FILES), required=True)
    args = parser.parse_args()

    run_started_at = time.perf_counter()
    rows = load_rows(args.mode)
    eligible_count = (
        len(rows)
        if args.mode == "vin"
        else sum(
            row.result.strip().casefold() in PROCESSABLE_RESULTS for row in rows
        )
    )
    print(
        f"Starting FieldPO Closer: loaded {len(rows)} {args.mode.upper()} row(s); "
        f"{eligible_count} eligible for processing.",
        flush=True,
    )
    needs_po_vin_lookup = args.mode == "po" and any(
        not row.vin.strip() for row in rows
    )
    if eligible_count == 0 and not needs_po_vin_lookup:
        print("Nothing to process. Eligible results are blank or Missing Invoice.", flush=True)
        return 0

    config = _load_config()
    account_name = str(config.get("account_name", "")).strip()
    if not account_name:
        raise ValueError(f"Missing account_name in {CONFIG_PATH}")

    pythoncom.CoInitialize()
    try:
        outlook_source = None
        invoices = []
        if eligible_count:
            print(
                "Searching Outlook Invoice, Processed, and Needs Review folders...",
                flush=True,
            )
            namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            outlook_source = OutlookInvoiceSource(
                namespace,
                account_name=account_name,
                attachment_directory=ATTACHMENT_DIR,
                pdf_parser=partial(
                    parse_invoice_pdf,
                    allow_missing_vin=args.mode == "po",
                    require_total=True,
                ),
            )
            archived_count = outlook_source.archive_expired_processed_invoices()
            print(
                f"[OUTLOOK] Archived {archived_count} expired Processed invoice(s) "
                "to Invoice_Archive.",
                flush=True,
            )
            scan = outlook_source.scan_parent_invoices(
                include_processed=True,
                include_needs_review=True,
                progress=lambda message: print(f"[OUTLOOK] {message}", flush=True),
            )
            invoices = list(scan.invoices)
            print(
                f"Outlook search complete: {len(scan.invoices)} valid invoice(s), "
                f"{len(scan.failures)} unreadable invoice(s) "
                f"({time.perf_counter() - run_started_at:.1f}s).",
                flush=True,
            )
            moved_unreadable = 0
            for failure in scan.failures:
                if not failure.internet_message_id:
                    raise ValueError(
                        "Cannot move an unreadable invoice to Needs Review without "
                        f"its InternetMessageID: {failure.subject}"
                    )
                outlook_source.move_to_needs_review(failure.internet_message_id)
                moved_unreadable += 1
            if moved_unreadable:
                print(
                    f"Moved {moved_unreadable} unreadable invoice(s) to Needs Review.",
                    flush=True,
                )
        else:
            print(
                "No invoice closeouts eligible; resolving blank VINs from FieldPO.",
                flush=True,
            )
        playwright_manager = None
        context = None
        page = None
        resolved_fieldpo_vins: dict[str, str] = {}

        def ensure_fieldpo_page() -> Any:
            nonlocal playwright_manager, context, page
            if page is None:
                print("Connecting to FieldPO...", flush=True)
                playwright_manager = sync_playwright()
                playwright = playwright_manager.__enter__()
                context, page = agn_invoices.connect_to_fieldpo(playwright)
                print("Connected to FieldPO.", flush=True)
            return page

        def resolve_fieldpo_vin(po_number: str) -> str:
            identity = FieldPOBrowserIdentityReader(ensure_fieldpo_page()).read_identity(po_number)
            resolved_fieldpo_vins[po_number] = identity.vin
            return identity.vin

        def process_invoices(
            matching_invoices: list[Any],
        ) -> str | dict[str, str]:
            current_page = ensure_fieldpo_page()
            fieldpo_vins = {
                str(invoice.subject_po_number).strip().upper(): resolved_fieldpo_vins.get(
                    str(invoice.subject_po_number).strip().upper(), ""
                )
                for invoice in matching_invoices
            }
            outcomes = process_fieldpo_invoices(
                current_page,
                matching_invoices,
                fieldpo_vins=fieldpo_vins if args.mode == "po" else None,
            )
            if args.mode == "vin":
                return outcomes
            return (
                "Success"
                if all(outcome == "Success" for outcome in outcomes.values())
                else "Review"
            )

        try:
            process_rows(
                args.mode,
                rows,
                invoices,
                process_invoices,
                lambda current: save_rows(args.mode, current),
                resolve_fieldpo_vin if args.mode == "po" else None,
                outlook_source.move_to_needs_review if outlook_source is not None else None,
                outlook_source.move_to_processed if outlook_source is not None else None,
            )
        finally:
            if context is not None:
                context.close()
            if playwright_manager is not None:
                playwright_manager.__exit__(None, None, None)
    finally:
        pythoncom.CoUninitialize()
        print(
            f"FieldPO Closer ended after {time.perf_counter() - run_started_at:.1f}s.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())