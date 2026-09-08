"""Build the reviewed Compass close queue from Outlook AGN invoices."""

import argparse
import csv
import json
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from vendor_tracking.monitor import _load_config
from vendor_tracking.sheet_updater import (
    STATUS_COMPLETED,
    VendorSheetUpdater,
    normalize_date_for_match,
)
from outlook.agn_invoices import mark_processed_category

DEFAULT_CLOSE_QUEUE = BASE_DIR / "WorkItems" / "close_workitem.csv"
DEFAULT_CLOSE_HISTORY = BASE_DIR / "data" / "close_workitem_history.csv"
PROCESSED_STATE_PATH = BASE_DIR / "data" / "close_workitem_processed.json"
INVOICE_SUBJECT_PATTERN = re.compile(r"invoice\s*#\s*(\d+)", re.IGNORECASE)


def _load_uncategorized_invoices() -> tuple[list[dict], list[str]]:
    """Read only uncategorized invoices directly from Outlook."""
    from outlook.agn_invoices import (
        extract_invoice_data,
        get_invoice_folder,
        has_processed_category,
        parse_received_timestamp,
    )

    folder = get_invoice_folder()
    items = sorted(
        list(folder.Items),
        key=lambda mail: parse_received_timestamp(str(getattr(mail, "ReceivedTime", ""))),
    )
    invoices: list[dict] = []
    review_notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="glass-close-invoices-") as temp_dir:
        for index, mail in enumerate(items):
            if getattr(mail, "Class", None) != 43 or has_processed_category(mail):
                continue
            subject = str(getattr(mail, "Subject", "") or "")
            if not INVOICE_SUBJECT_PATTERN.search(subject):
                continue
            attachments = [
                attachment
                for attachment in mail.Attachments
                if str(attachment.FileName).lower().endswith(".pdf")
            ]
            if not attachments:
                review_notes.append(f"{subject}: no PDF attachment")
                continue
            pdf_path = Path(temp_dir) / f"invoice-{index}.pdf"
            attachments[0].SaveAsFile(str(pdf_path))
            invoice_date, vin, amount = extract_invoice_data(str(pdf_path))
            if not vin or not amount:
                review_notes.append(f"{subject}: PDF VIN or amount could not be parsed")
                continue
            invoices.append({
                "subject": subject,
                "received": str(getattr(mail, "ReceivedTime", "")),
                "invoice_date": invoice_date.isoformat() if invoice_date else "",
                "vin": vin,
                "invoice_amount": amount,
                "mail": mail,
            })
    return invoices, review_notes


def _load_existing_candidates(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as queue_file:
        reader = csv.DictReader(line for line in queue_file if not line.startswith("#"))
        return {
            (str(row.get("mva") or "").strip(), str(row.get("Type") or "").strip())
            for row in reader
        }


def _load_processed_candidates(path: Path) -> set[tuple[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if payload.get("date") != date.today().isoformat():
        return set()
    return {
        (str(item.get("mva") or "").strip(), str(item.get("complaint_type") or "").strip())
        for item in payload.get("processed", [])
    }


def _retire_processed_candidates(
    close_path: Path,
    history_path: Path,
    processed: set[tuple[str, str]],
) -> int:
    if not close_path.exists() or not processed:
        return 0

    comments: list[str] = []
    with close_path.open(encoding="utf-8") as close_file:
        for line in close_file:
            if line.startswith("#") or not line.strip():
                comments.append(line)
            else:
                break
    with close_path.open(newline="", encoding="utf-8") as close_file:
        rows = list(csv.DictReader(line for line in close_file if not line.startswith("#")))

    retired = [
        row for row in rows
        if (str(row.get("mva") or "").strip(), str(row.get("Type") or "").strip()) in processed
    ]
    if not retired:
        return 0
    remaining = [row for row in rows if row not in retired]

    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_exists = history_path.exists() and history_path.stat().st_size > 0
    with history_path.open("a", newline="", encoding="utf-8") as history_file:
        writer = csv.DictWriter(
            history_file,
            fieldnames=["mva", "Type", "processed_date", "result"],
        )
        if not history_exists:
            writer.writeheader()
        for row in retired:
            writer.writerow({
                "mva": str(row.get("mva") or "").strip(),
                "Type": str(row.get("Type") or "").strip(),
                "processed_date": date.today().isoformat(),
                "result": "checkpointed",
            })

    with close_path.open("w", newline="", encoding="utf-8") as close_file:
        close_file.writelines(comments)
        writer = csv.DictWriter(close_file, fieldnames=["mva", "Type"])
        writer.writeheader()
        for row in remaining:
            writer.writerow({"mva": row.get("mva", ""), "Type": row.get("Type", "")})
    return len(retired)


def _append_candidates(path: Path, candidates: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as queue_file:
        writer = csv.DictWriter(queue_file, fieldnames=["mva", "Type"])
        if needs_header:
            writer.writeheader()
        for candidate in candidates:
            writer.writerow({"mva": candidate["mva"], "Type": candidate["complaint_type"]})


def _normalize_glass_candidate(raw_mva: str) -> tuple[str, str] | None:
    mva = raw_mva.strip()
    if len(mva) == 8 and mva.isdigit():
        mva = f"0{mva}"
    if len(mva) != 9 or not mva.isdigit():
        return None
    return mva, "Glass"


def _sheet_date_sort_key(raw_date: str) -> date:
    normalized = normalize_date_for_match(raw_date)
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return date.min


def _select_invoice_sheet_row(
    updater: VendorSheetUpdater,
    vin: str,
) -> tuple[int | None, tuple[str, str] | None, str]:
    row_indexes = updater.find_rows_by_vin(vin)
    if not row_indexes:
        return None, None, f"VIN {vin} not found"

    rows: list[tuple[int, dict[str, str], tuple[str, str] | None]] = []
    for row_index in row_indexes:
        fields = updater.get_row_fields(
            row_index,
            ["MVA", "Inventory Date", "Arrival Date"],
        )
        rows.append((row_index, fields, _normalize_glass_candidate(fields.get("MVA", ""))))

    if any(normalized is None for _row_index, _fields, normalized in rows):
        return None, None, f"VIN {vin} has an invalid MVA on one or more Sheet rows"

    normalized_candidates = {normalized for _row_index, _fields, normalized in rows}
    if len(normalized_candidates) != 1:
        mvas = sorted(candidate[0] for candidate in normalized_candidates if candidate is not None)
        return None, None, f"VIN {vin} matched different MVAs: {', '.join(mvas)}"

    selected = max(
        rows,
        key=lambda row: (
            _sheet_date_sort_key(
                row[1].get("Inventory Date", "") or row[1].get("Arrival Date", "")
            ),
            row[0],
        ),
    )
    return selected[0], selected[2], ""


def build_close_candidates(
    updater: VendorSheetUpdater,
    invoices: list[dict],
    existing: set[tuple[str, str]],
    max_rows: int | None,
    target_mvas: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    candidates: list[dict] = []
    review_notes: list[str] = []
    proposed = set(existing)
    new_count = 0

    for invoice in invoices:
        subject = invoice.get("subject", "")
        vin = invoice.get("vin", "")
        invoice_match = INVOICE_SUBJECT_PATTERN.search(subject)
        invoice_id = invoice_match.group(1) if invoice_match else ""
        row_index, normalized, match_note = _select_invoice_sheet_row(updater, vin)
        if row_index is None or normalized is None:
            if target_mvas is None:
                review_notes.append(f"{subject}: {match_note}")
            continue

        mva, complaint_type = normalized
        if target_mvas is not None and mva not in target_mvas:
            continue
        key = (mva, complaint_type)
        if key in proposed:
            continue
        if max_rows is not None and new_count >= max_rows:
            continue
        proposed.add(key)
        new_count += 1

        candidate = {
            "mva": mva,
            "complaint_type": complaint_type,
            "vin": vin,
            "invoice_id": invoice_id,
            "cost": invoice.get("invoice_amount", ""),
            "row_index": str(row_index),
            "received": invoice.get("received", ""),
        }
        if "mail" in invoice:
            candidate["mail"] = invoice["mail"]
        candidates.append(candidate)

    return candidates, review_notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a reviewed close queue from Outlook AGN invoices."
    )
    parser.add_argument("--apply", action="store_true", help="Write candidates to the close CSV and mark matched Sheet rows Completed")
    parser.add_argument("--reconcile-only", action="store_true", help="Archive checkpointed CSV rows without selecting or adding candidates")
    parser.add_argument("--max-rows", type=int, default=None, help="Limit new candidates after oldest-first matching")
    parser.add_argument("--mva", action="append", default=[], help="Limit review to one MVA; may be repeated")
    parser.add_argument("--close-queue", type=Path, default=DEFAULT_CLOSE_QUEUE)
    parser.add_argument("--history", type=Path, default=DEFAULT_CLOSE_HISTORY)
    args = parser.parse_args()
    if args.max_rows is not None and args.max_rows <= 0:
        parser.error("--max-rows must be greater than 0")
    target_mvas: set[str] | None = None
    if args.mva:
        normalized_targets = [_normalize_glass_candidate(raw_mva) for raw_mva in args.mva]
        if any(target is None for target in normalized_targets):
            parser.error("--mva must contain an 8- or 9-digit MVA")
        target_mvas = {target[0] for target in normalized_targets if target is not None}

    if args.reconcile_only:
        processed = _load_processed_candidates(PROCESSED_STATE_PATH)
        retired_count = _retire_processed_candidates(args.close_queue, args.history, processed)
        print(f"Retired {retired_count} checkpointed candidate(s) from the review queue")
        return 0

    config = _load_config()
    spreadsheet_id = str(config.get("vendor_tracking_spreadsheet_id") or config.get("spreadsheet_id", ""))
    sheet_name = str(config.get("vendor_tracking_sheet_name") or config.get("sheet_name", "GlassClaims"))
    service_account = Path(str(config.get("service_account_json", "Service_account.json")))
    if not service_account.is_absolute():
        service_account = BASE_DIR / service_account
    if not spreadsheet_id:
        print("ERROR: spreadsheet_id is not configured.")
        return 1

    updater = VendorSheetUpdater(spreadsheet_id, sheet_name, str(service_account))
    updater.connect()
    if args.apply:
        updater.ensure_columns()
    invoices, source_review_notes = _load_uncategorized_invoices()
    processed = _load_processed_candidates(PROCESSED_STATE_PATH)
    queued = _load_existing_candidates(args.close_queue)
    completed = _load_existing_candidates(args.history) | processed
    existing = queued | completed
    candidates, review_notes = build_close_candidates(
        updater,
        invoices,
        existing,
        args.max_rows,
        target_mvas=target_mvas,
    )
    review_notes = source_review_notes + review_notes

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: {len(invoices)} invoice(s), {len(candidates)} new candidate(s)")
    for candidate in candidates:
        print(
            f"  {candidate['received']} | MVA {candidate['mva']} | "
            f"VIN {candidate['vin']} | Invoice {candidate['invoice_id']}"
        )
    if review_notes:
        print(f"Needs review: {len(review_notes)} invoice(s) could not be matched safely")
        for note in review_notes[:20]:
            print(f"  {note}")

    if not args.apply:
        print("No files or Sheet rows were changed. Re-run with --apply after reviewing this list.")
        return 0

    retired_count = _retire_processed_candidates(args.close_queue, args.history, processed)
    if retired_count:
        print(f"Retired {retired_count} handled candidate(s) from the review queue")
    _append_candidates(args.close_queue, candidates)
    for candidate in candidates:
        fields = {"Repair Status": STATUS_COMPLETED}
        if candidate["cost"]:
            fields["Cost"] = candidate["cost"]
        updater.update_vendor_fields(int(candidate["row_index"]), fields)
        mail = candidate.get("mail")
        if mail is not None:
            mark_processed_category(mail)
    print(f"Added {len(candidates)} candidate(s) to {args.close_queue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())