"""
agn_invoices.py

All-in-one AGN invoice automation.

Run it with:
    python agn_invoices.py

What it does, in order:
  1. EXTRACT   - reads Inbox\\AGN\\Invoice in Outlook, saves each new PDF,
                 pulls out the VIN + invoice amount, adds to invoices_queue.csv,
                 moves the email into Inbox\\AGN\\Invoice\\Processed
  2. CHECK     - opens FieldPO in a browser, looks up each queued VIN,
                 and reads whether it's already APPROVED (skip) or still
                 pending, comparing the auth amount to the invoice amount
  3. APPROVE   - shows you a review list of everything that matched,
                 waits for you to type "yes" once, then clicks APPROVE
                 on all of them. Mismatches/errors are never auto-approved.

First-time setup (one time only):
    pip install -r requirements.txt
    playwright install chromium
    python agn_invoices.py --setup-credentials

Everything is stored under: %USERPROFILE%\\AGN_Automation\\
    pdfs\\                  downloaded invoice PDFs
    invoices_queue.csv      the working queue (safe to open in Excel any time)
    browser_profile\\       saved FieldPO login session

SELECTORS: the FieldPO click-path (search, click vehicle, Active Work Order
tab, PO card, auth amount, APPROVE button) is marked with TODO comments
below. These were built from screenshots, not the live page, so the first
real run will likely need one or two of them corrected -- run it, see where
it stops, right-click that element in the browser -> Inspect, and update
the matching selector here.
"""

import os
import re
import csv
import sys
import json
import getpass
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

import win32com.client
import pdfplumber
import keyring
from playwright.sync_api import sync_playwright


class Tee:
    """Writes to multiple streams at once (e.g. console + log file)."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


# ============================================================
# CONFIG - adjust these to match your setup
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = SCRIPT_DIR
ATTACHMENT_DIR = os.path.join(BASE_DIR, "pdfs")
QUEUE_CSV = os.path.join(BASE_DIR, "invoices_queue.csv")
DEFAULT_PO_LIST_FILE = os.path.join(BASE_DIR, "po_numbers.txt")
DIRECT_APPROVAL_LOG_FILE = os.path.join(BASE_DIR, "direct_approval_results.jsonl")
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")
CONFIG_FILE = os.path.join(BASE_DIR, "agn_invoices_config.json")

LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")
DECISION_LOG_FILE = os.path.join(BASE_DIR, "closure_decisions.jsonl")


def _load_runtime_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except Exception as exc:
        print(f"WARNING: Could not read config '{CONFIG_FILE}': {exc}")
    return {}


def _int_or_default(value, default):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_excluded_standard_price_skus(payload):
    values = payload.get("excluded_standard_price_skus")
    if not isinstance(values, list) or not values:
        raise ValueError(
            "Config field 'excluded_standard_price_skus' must be a non-empty list"
        )

    normalized = []
    seen = set()
    for value in values:
        sku = str(value or "").strip().upper()
        if sku.endswith("GTYN"):
            sku = sku[:-1]
        if not re.fullmatch(r"[DF]W\d{5}GTY", sku):
            raise ValueError(
                f"Invalid excluded standard-price SKU in {CONFIG_FILE}: {value!r}"
            )
        if sku in seen:
            raise ValueError(
                f"Duplicate excluded standard-price SKU in {CONFIG_FILE}: {sku}"
            )
        seen.add(sku)
        normalized.append(sku)
    return frozenset(normalized)


RUNTIME_CONFIG = _load_runtime_config()

ACCOUNT_NAME = str(RUNTIME_CONFIG.get("account_name", "Dirk.Steele@avisbudget.com"))

PROCESSED_FOLDER_NAME = str(RUNTIME_CONFIG.get("processed_folder_name", "Processed"))
PROCESSED_CATEGORY_NAME = str(RUNTIME_CONFIG.get("processed_category_name", "Green Category"))

# Green is a legacy ingestion marker. Categorized invoices still in Invoice must
# be rechecked before migration because the category did not guarantee closure.
PROCESS_CATEGORIZED_INVOICES = bool(
    RUNTIME_CONFIG.get("process_categorized_invoices", False)
)

# Safety limit for the EXTRACT step. None = no limit.
# Keep this small until you trust the output, then raise it or set to None.
MAX_ITEMS_PER_RUN = _int_or_default(RUNTIME_CONFIG.get("max_items_per_run", 3), 3)

# Safety limit for the APPROVE step. None = no limit.
# Keep this at 1 for your first real approval, then raise it once confirmed working.
MAX_APPROVALS_PER_RUN = _int_or_default(RUNTIME_CONFIG.get("max_approvals_per_run", 1), 1)

# Minimum invoice age required by both dry-run and live closure gates.
AGE_APPROVAL_DAYS = _int_or_default(RUNTIME_CONFIG.get("age_approval_days", 21), 21)

# Only approve/close when the Work Order's Created By field matches one of these names.
_allowed_work_order_created_by_raw = RUNTIME_CONFIG.get(
    "allowed_work_order_created_by",
    RUNTIME_CONFIG.get("allowed_created_by", []),
)
if isinstance(_allowed_work_order_created_by_raw, str):
    _allowed_work_order_created_by_raw = [_allowed_work_order_created_by_raw]
ALLOWED_WORK_ORDER_CREATED_BY = [
    str(value).strip()
    for value in _allowed_work_order_created_by_raw
    if str(value).strip()
]

# Backward compatibility: legacy single-name setting.
if not ALLOWED_WORK_ORDER_CREATED_BY:
    _required_created_by = str(RUNTIME_CONFIG.get("required_created_by", "")).strip()
    if _required_created_by:
        ALLOWED_WORK_ORDER_CREATED_BY = [_required_created_by]

FIELDPO_URL = "https://supply-chain.east.prod.sdp.abg.cloud/fieldpo/dashboard"

CREDENTIAL_SERVICE_NAME = "AGN_Automation_FieldPO"

QUEUE_FIELDS = [
    "subject", "received", "po_number", "vin", "invoice_amount", "pdf_path",
    "status", "auth_amount", "match",
]

QUEUE_STATUS_BUCKETS = {
    "approved": {"approved"},
    "failed": {"approve_failed", "error"},
    "skipped": {"already_paid", "no_po", "creator_mismatch", "identity_mismatch"},
    "processed": {"new", "checked"},
}

EXCLUDED_STANDARD_PRICE_SKUS = _load_excluded_standard_price_skus(RUNTIME_CONFIG)


# ============================================================
# CREDENTIALS (Windows Credential Manager via keyring - no plaintext)
# ============================================================

def set_fieldpo_credentials():
    print("Setting FieldPO credentials.")
    print("Password is hidden as you type and stored securely in Windows")
    print("Credential Manager -- not in any file.\n")
    username = input("FieldPO username (e.g. your email): ").strip()
    password = getpass.getpass("FieldPO password: ")
    keyring.set_password(CREDENTIAL_SERVICE_NAME, username, password)
    keyring.set_password(CREDENTIAL_SERVICE_NAME, "__last_username__", username)
    print(f"\nStored credentials for '{username}'.")


def get_fieldpo_credentials():
    username = keyring.get_password(CREDENTIAL_SERVICE_NAME, "__last_username__")
    if not username:
        return None, None
    password = keyring.get_password(CREDENTIAL_SERVICE_NAME, username)
    return username, password


# ============================================================
# STEP 1: EXTRACT (Outlook)
# ============================================================

def get_invoice_folder():
    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    for account_folder in outlook.Folders:
        if account_folder.Name.lower() == ACCOUNT_NAME.lower():
            inbox = account_folder.Folders["Inbox"]
            agn = inbox.Folders["AGN"]
            return agn.Folders["Invoice"]
    raise RuntimeError(
        f"Could not find account '{ACCOUNT_NAME}' in Outlook. "
        "Check ACCOUNT_NAME at the top of this script matches your Outlook folder pane."
    )


def get_or_create_processed_folder(invoice_folder):
    for f in invoice_folder.Folders:
        if f.Name == PROCESSED_FOLDER_NAME:
            return f
    return invoice_folder.Folders.Add(PROCESSED_FOLDER_NAME)


def _split_categories(raw_categories):
    return [c.strip() for c in re.split(r"[,;]", (raw_categories or "")) if c.strip()]


def _extract_subject_id(subject):
    match = re.search(r"(?:Invoice|Job)\s*#(\d+)", subject or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_subject_po_number(subject):
    match = re.fullmatch(
        r"\[External\]\s+Invoice\s+#\d+\s+\(PO\s+#\s*(FPO\d+)\)",
        (subject or "").strip(),
        re.IGNORECASE,
    )
    return match.group(1).upper() if match else ""


def _build_subject_po_filter(po_numbers):
    normalized_po_numbers = sorted({str(po).strip().upper() for po in po_numbers})
    if not normalized_po_numbers or any(
        not re.fullmatch(r"FPO\d+", po) for po in normalized_po_numbers
    ):
        raise ValueError("Subject PO filter requires one or more valid FPO numbers")
    clauses = [
        f'"urn:schemas:httpmail:subject" ci_phrasematch \'PO # {po}\''
        for po in normalized_po_numbers
    ]
    return f"@SQL=({' OR '.join(clauses)})"


def _queue_identity_key(subject, vin, amount):
    subject_id = _extract_subject_id(subject)
    normalized_vin = (vin or "").strip().upper()
    normalized_amount = (amount or "").strip()
    if subject_id:
        return f"id:{subject_id}|vin:{normalized_vin}|amt:{normalized_amount}"
    normalized_subject = (subject or "").strip().lower()
    return f"subject:{normalized_subject}|vin:{normalized_vin}|amt:{normalized_amount}"


def has_processed_category(mail):
    categories = _split_categories(getattr(mail, "Categories", ""))
    return any(PROCESSED_CATEGORY_NAME.lower() == category.lower() for category in categories)


def extract_invoice_data(pdf_path):
    """Extract the invoice date, VIN, and subtotal from an AGN invoice PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not re.search(r"(?im)^Invoice\s+#\d+\s*$", full_text):
        return None, None, None

    date_match = re.search(r"(?m)^\s*(\d{4}-\d{2}-\d{2})\s*$", full_text)
    try:
        invoice_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date() if date_match else None
    except ValueError:
        invoice_date = None

    vin_match = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", full_text)
    vin = vin_match.group(1) if vin_match else None

    amount_match = re.search(r"Subtotal\s*\$?([\d,]+\.\d{2})", full_text)
    amount = amount_match.group(1).replace(",", "") if amount_match else None

    return invoice_date, vin, amount


def extract_invoice_po_number(pdf_path):
    """Extract the exact FieldPO purchase-order number from an AGN invoice PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    match = re.search(r"\bPO\s*#\s*(FPO\d+)\b", full_text, re.IGNORECASE)
    return match.group(1).upper() if match else None


def normalize_invoice_sku(value):
    sku = str(value or "").strip().upper()
    return sku[:-1] if sku.endswith("GTYN") else sku


def extract_invoice_sku(pdf_path):
    """Extract and normalize the windshield SKU from an AGN invoice PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    match = re.search(r"\b([DF]W\d{5}GTYN?)\b", full_text, re.IGNORECASE)
    return normalize_invoice_sku(match.group(1)) if match else None


def load_po_numbers(path):
    """Load a strict, de-duplicated FPO allowlist from a text file."""
    po_numbers = []
    seen = set()
    with open(path, encoding="utf-8-sig") as po_file:
        for line_number, raw_line in enumerate(po_file, start=1):
            value = raw_line.strip().upper()
            if not value or value.startswith("#"):
                continue
            if not re.fullmatch(r"FPO\d+", value):
                raise ValueError(
                    f"Invalid PO number in {path} line {line_number}: {raw_line.strip()}"
                )
            if value not in seen:
                seen.add(value)
                po_numbers.append(value)
    if not po_numbers:
        raise ValueError(f"PO list is empty: {path}")
    return po_numbers


def load_direct_approval_targets(path):
    """Load strict MVA, VIN, and PO authorization triples from a TSV file."""
    with open(path, newline="", encoding="utf-8-sig") as target_file:
        reader = csv.DictReader(target_file, delimiter="\t")
        expected_fields = ["MVA_NUMBER", "VIN_NO", "PO_NUMBER"]
        if reader.fieldnames != expected_fields:
            raise ValueError(
                f"Direct approval file must have tab-separated headers: {', '.join(expected_fields)}"
            )
        targets = []
        seen_po_numbers = set()
        for line_number, row in enumerate(reader, start=2):
            mva = normalize_mva(row.get("MVA_NUMBER", ""))
            vin = normalize_vin(row.get("VIN_NO", ""))
            po_number = str(row.get("PO_NUMBER", "")).strip().upper()
            if not mva:
                raise ValueError(f"Invalid MVA in {path} line {line_number}")
            if not is_valid_vin(vin):
                raise ValueError(f"Invalid VIN in {path} line {line_number}")
            if not re.fullmatch(r"FPO\d+", po_number):
                raise ValueError(f"Invalid PO number in {path} line {line_number}")
            if po_number in seen_po_numbers:
                raise ValueError(f"Duplicate PO number in {path} line {line_number}: {po_number}")
            seen_po_numbers.add(po_number)
            targets.append({"mva": mva, "vin": vin, "po_number": po_number})
    if not targets:
        raise ValueError(f"Direct approval file is empty: {path}")
    return targets


def validate_invoice_target(target, invoice_vin, fieldpo_mva=None):
    """Require the invoice VIN and FieldPO MVA to match one authorized target."""
    if normalize_vin(invoice_vin) != target["vin"]:
        raise LookupError(
            f"Invoice VIN mismatch for {target['po_number']}: "
            f"found {normalize_vin(invoice_vin) or 'none'}, expected {target['vin']}"
        )
    normalized_mva = normalize_mva(fieldpo_mva) if fieldpo_mva is not None else None
    if normalized_mva is not None and normalized_mva != target["mva"]:
        raise LookupError(
            f"FieldPO MVA mismatch for {target['po_number']}: "
            f"found {normalized_mva or 'none'}, expected {target['mva']}"
        )


def extract_vin_and_amount(pdf_path):
    """Backward-compatible VIN and subtotal extraction."""
    _, vin, amount = extract_invoice_data(pdf_path)
    return vin, amount


def extract_receipt_vin_and_amount(pdf_path):
    """Extract VIN and final total from an AGN Job receipt PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if not re.search(r"(?im)^Job\s+#\d+\s*$", full_text):
        return None, None
    vin_match = re.search(r"\bVIN\s+([A-HJ-NPR-Z0-9]{17})\b", full_text, re.IGNORECASE)
    total_matches = re.findall(r"(?im)^Total\s+\$?([\d,]+\.\d{2})\s*$", full_text)
    vin = vin_match.group(1).upper() if vin_match else None
    amount = total_matches[-1].replace(",", "") if total_matches else None
    return vin, amount


def _parse_amount(value):
    try:
        return Decimal(str(value or "").replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def evaluate_closure(
    invoice_date,
    invoice_amount,
    auth_amount,
    mva,
    work_order_created_by="",
    system_date=None,
    min_age_days=None,
    allow_price_mismatch=False,
):
    """Return a read-only closure decision and all failed gate reasons."""
    today = system_date or date.today()
    normalized_mva = normalize_mva(mva)
    invoice_price = _parse_amount(invoice_amount)
    fieldpo_price = _parse_amount(auth_amount)
    age_days = (today - invoice_date).days if isinstance(invoice_date, date) else None
    reasons = []

    if not normalized_mva:
        reasons.append("invalid_or_missing_mva")
    if not str(work_order_created_by or "").strip():
        reasons.append("missing_work_order_created_by")
    elif not is_allowed_work_order_creator(work_order_created_by):
        reasons.append("work_order_created_by_mismatch")
    if invoice_date is None:
        reasons.append("missing_invoice_date")
    elif min_age_days is not None and age_days < min_age_days:
        reasons.append("invoice_too_new")
    if invoice_price is None or fieldpo_price is None:
        reasons.append("invalid_or_missing_price")
    elif invoice_price != fieldpo_price and not allow_price_mismatch:
        reasons.append("price_mismatch")

    return {
        "decision": "WOULD_CLOSE" if not reasons else "SKIPPED",
        "reasons": reasons,
        "mva": normalized_mva,
        "invoice_age_days": age_days,
        "price_match": invoice_price is not None and invoice_price == fieldpo_price,
    }


def parse_received_timestamp(value):
    """Parse Outlook-style timestamp strings for reliable oldest-first sorting."""
    raw = (value or "").strip()
    if not raw:
        return datetime.max

    for parser in (
        lambda v: datetime.fromisoformat(v),
        lambda v: parsedate_to_datetime(v),
    ):
        try:
            parsed = parser(raw)
            if parsed.tzinfo is not None:
                return parsed.astimezone().replace(tzinfo=None)
            return parsed
        except Exception:
            continue

    return datetime.max


def normalize_vin(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def is_valid_vin(value):
    return re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", normalize_vin(value)) is not None


def normalize_mva(value):
    normalized = re.sub(r"\s+", "", str(value or ""))
    if len(normalized) == 8 and normalized.isdigit():
        return f"0{normalized}"
    if len(normalized) == 9 and normalized.isdigit():
        return normalized
    return ""


def parse_trace_vins(raw_values):
    vins = []
    seen = set()
    invalid = []

    for raw in raw_values or []:
        for token in re.split(r"[\s,;]+", raw.strip()):
            if not token:
                continue
            normalized = normalize_vin(token)
            if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", normalized):
                if normalized not in seen:
                    seen.add(normalized)
                    vins.append(normalized)
            else:
                invalid.append(token)

    return vins, invalid


def queue_status_to_bucket(status):
    normalized = (status or "").strip().lower()
    for bucket, statuses in QUEUE_STATUS_BUCKETS.items():
        if normalized in statuses:
            return bucket
    return "processed"


def summarize_vin_status(queue_hits, outlook_hits):
    if not queue_hits:
        return "processed" if outlook_hits else "missing"

    queue_buckets = {queue_status_to_bucket(row.get("status", "")) for row in queue_hits}
    for preferred in ("approved", "failed", "skipped", "processed"):
        if preferred in queue_buckets:
            return preferred
    return "processed"


def iter_outlook_folders(root_folder):
    stack = [(root_folder, root_folder.Name)]
    while stack:
        folder, folder_path = stack.pop()
        yield folder, folder_path
        for subfolder in folder.Folders:
            stack.append((subfolder, f"{folder_path}\\{subfolder.Name}"))


def collect_outlook_vin_hits(vins, vin_subject_ids=None):
    hits = {vin: [] for vin in vins}
    invoice_folder = get_invoice_folder()
    vin_subject_ids = vin_subject_ids or {}

    for folder, folder_path in iter_outlook_folders(invoice_folder):
        try:
            items = folder.Items
        except Exception:
            continue

        for item in items:
            try:
                if item.Class != 43:
                    continue

                subject = getattr(item, "Subject", "") or ""
                subject_upper = subject.upper()
                subject_id = _extract_subject_id(subject)

                try:
                    body_upper = (getattr(item, "Body", "") or "").upper()
                except Exception:
                    body_upper = ""

                attachment_names = []
                for attachment in getattr(item, "Attachments", []):
                    attachment_names.append(getattr(attachment, "FileName", "") or "")
                attachment_text = " ".join(attachment_names).upper()

                for vin in vins:
                    sources = []
                    if vin in subject_upper:
                        sources.append("subject")
                    if vin in body_upper:
                        sources.append("body")
                    if vin in attachment_text:
                        sources.append("attachment_name")
                    if subject_id and subject_id in vin_subject_ids.get(vin, set()):
                        sources.append("subject_id")
                    if not sources:
                        continue

                    hits[vin].append({
                        "folder": folder_path,
                        "subject": subject,
                        "received": str(getattr(item, "ReceivedTime", "")),
                        "categorized_processed": has_processed_category(item),
                        "sources": ", ".join(sources),
                    })
            except Exception:
                continue

    return hits


def step_trace_vins(vins):
    print("\n" + "=" * 60)
    print("VIN TRACE REPORT")
    print("=" * 60)

    queue_rows = read_queue()
    queue_hits = {vin: [] for vin in vins}
    vin_subject_ids = {vin: set() for vin in vins}
    for row in queue_rows:
        row_vin = normalize_vin(row.get("vin", ""))
        if row_vin in queue_hits:
            queue_hits[row_vin].append(row)
            subject_id = _extract_subject_id(row.get("subject", ""))
            if subject_id:
                vin_subject_ids[row_vin].add(subject_id)

    print("Searching Outlook AGN\\Invoice folders for VIN matches...")
    outlook_hits = collect_outlook_vin_hits(vins, vin_subject_ids=vin_subject_ids)

    summary_counts = {"processed": 0, "skipped": 0, "approved": 0, "failed": 0, "missing": 0}

    for vin in vins:
        q_hits = queue_hits.get(vin, [])
        o_hits = outlook_hits.get(vin, [])
        final_status = summarize_vin_status(q_hits, o_hits)
        summary_counts[final_status] += 1

        print("\n" + "-" * 60)
        print(f"VIN: {vin}")
        print(f"TRACE STATUS: {final_status.upper()}")
        print(f"Queue matches: {len(q_hits)} | Outlook email matches: {len(o_hits)}")

        if q_hits:
            print("Queue history:")
            sorted_hits = sorted(q_hits, key=lambda r: parse_received_timestamp(r.get("received", "")))
            for row in sorted_hits:
                print(
                    "  "
                    f"[{row.get('status', '')}] "
                    f"{row.get('received', '')} | "
                    f"Amt ${row.get('invoice_amount', '')} | "
                    f"Auth ${row.get('auth_amount', '') or '-'} | "
                    f"Match={row.get('match', '') or '-'} | "
                    f"{row.get('subject', '')}"
                )
        else:
            print("Queue history: no matches")

        if o_hits:
            print("Outlook invoice email matches:")
            sorted_mail_hits = sorted(o_hits, key=lambda h: parse_received_timestamp(h.get("received", "")))
            for hit in sorted_mail_hits:
                processed_flag = "yes" if hit.get("categorized_processed") else "no"
                print(
                    "  "
                    f"[{hit.get('folder', '')}] "
                    f"{hit.get('received', '')} | "
                    f"categorized_processed={processed_flag} | "
                    f"source={hit.get('sources', '')} | "
                    f"{hit.get('subject', '')}"
                )
        else:
            print("Outlook invoice email matches: no matches")

    print("\n" + "=" * 60)
    print(
        "Trace summary: "
        f"approved={summary_counts['approved']}, "
        f"failed={summary_counts['failed']}, "
        f"skipped={summary_counts['skipped']}, "
        f"processed={summary_counts['processed']}, "
        f"missing={summary_counts['missing']}"
    )
    print("=" * 60)


def read_queue():
    if not os.path.isfile(QUEUE_CSV):
        return []
    with open(QUEUE_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_to_queue(rows):
    if not rows:
        return
    file_exists = os.path.isfile(QUEUE_CSV)
    with open(QUEUE_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in QUEUE_FIELDS}
            for row in rows
        )


def write_queue(rows):
    if not rows:
        return
    with open(QUEUE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in QUEUE_FIELDS}
            for row in rows
        )


def step_extract(max_invoices=None, po_numbers=None, target_by_po=None):
    print("\n" + "=" * 60)
    print("STEP 1: EXTRACT - reading Outlook AGN\\Invoice folder")
    print("=" * 60)

    os.makedirs(ATTACHMENT_DIR, exist_ok=True)
    os.makedirs(BASE_DIR, exist_ok=True)

    invoice_folder = get_invoice_folder()
    if po_numbers:
        all_items = []
        subject_filter = _build_subject_po_filter(po_numbers)
        for folder, folder_path in iter_outlook_folders(invoice_folder):
            try:
                all_items.extend(list(folder.Items.Restrict(subject_filter)))
            except Exception as exc:
                raise RuntimeError(
                    f"Could not search Outlook folder '{folder_path}' by PO subject"
                ) from exc
        print("Searching AGN\\Invoice and all subfolders for exact PO matches.")
    else:
        items = invoice_folder.Items
        items.Sort("[ReceivedTime]", False)  # oldest first
        all_items = list(items)  # copy before moving anything
    all_items.sort(
        key=lambda mail: (
            has_processed_category(mail) if PROCESS_CATEGORIZED_INVOICES else False,
            parse_received_timestamp(str(getattr(mail, "ReceivedTime", ""))),
        )
    )

    existing_rows = read_queue()
    existing_by_key = {
        _queue_identity_key(
            row.get("subject", ""),
            row.get("vin", ""),
            row.get("invoice_amount", ""),
        ): row
        for row in existing_rows
    }

    invoice_limit = MAX_ITEMS_PER_RUN if max_invoices is None else max_invoices
    allowed_po_numbers = set(po_numbers or [])
    rows = []
    new_queue_rows = []
    reused_queue_row = False
    for mail in all_items:
        if invoice_limit is not None and len(rows) >= invoice_limit:
            print(f"Reached invoice limit ({invoice_limit}). Stopping extraction for this run.")
            break
        try:
            if mail.Class != 43:  # olMail only
                continue

            is_categorized = has_processed_category(mail)
            if is_categorized and not PROCESS_CATEGORIZED_INVOICES:
                continue

            if re.search(r"receipt\s+for\s+job\s*#", str(mail.Subject or ""), re.IGNORECASE):
                continue

            subject_po_number = _extract_subject_po_number(mail.Subject)
            if allowed_po_numbers and subject_po_number not in allowed_po_numbers:
                continue

            pdf_attachments = [a for a in mail.Attachments if a.FileName.lower().endswith(".pdf")]
            if not pdf_attachments:
                print(f"Skipping '{mail.Subject}' -- no PDF attachment.")
                continue

            attachment = pdf_attachments[0]
            safe_subject = re.sub(r"[^\w\-]", "_", mail.Subject)[:60]
            pdf_path = os.path.join(ATTACHMENT_DIR, f"{safe_subject}_{mail.EntryID[-8:]}.pdf")
            attachment.SaveAsFile(pdf_path)

            vin, amount = extract_vin_and_amount(pdf_path)
            if not vin or not amount:
                print(f"WARNING: Could not parse '{mail.Subject}' (VIN={vin}, amount={amount}). "
                      "Leaving email in place for manual review.")
                continue

            po_number = subject_po_number or None
            if allowed_po_numbers:
                if target_by_po is not None:
                    try:
                        validate_invoice_target(target_by_po[po_number], vin)
                    except LookupError as exc:
                        print(f"WARNING: {exc}. Invoice will not be queued.")
                        continue

            identity_key = _queue_identity_key(mail.Subject, vin, amount)
            existing_row = existing_by_key.get(identity_key)
            if existing_row is not None:
                existing_row.update({
                    "subject": mail.Subject,
                    "received": str(mail.ReceivedTime),
                    "po_number": po_number or "",
                    "vin": vin,
                    "invoice_amount": amount,
                    "pdf_path": pdf_path,
                    "status": "new",
                    "auth_amount": "",
                    "match": "",
                })
                rows.append({
                    **existing_row,
                    "_outlook_entry_id": str(mail.EntryID),
                })
                reused_queue_row = True
                print(
                    f"Re-queued existing invoice: {mail.Subject} -> VIN {vin}, ${amount}"
                )
                continue

            queue_row = {
                "subject": mail.Subject,
                "received": str(mail.ReceivedTime),
                "po_number": po_number or "",
                "vin": vin,
                "invoice_amount": amount,
                "pdf_path": pdf_path,
                "status": "new",
                "auth_amount": "",
                "match": "",
                "_outlook_entry_id": str(mail.EntryID),
            }

            rows.append(queue_row)
            new_queue_rows.append(queue_row)
            existing_by_key[identity_key] = queue_row
            print(f"Extracted: {mail.Subject} -> VIN {vin}, ${amount}")

        except Exception as e:
            print(f"ERROR processing an email: {e}")

    if not rows:
        print("No new invoices found.")
        return []

    if reused_queue_row:
        write_queue(existing_rows + new_queue_rows)
    else:
        append_to_queue(new_queue_rows)
    print(f"\n{len(rows)} invoice(s) added to queue: {QUEUE_CSV}")
    return rows


# ============================================================
# STEP 2: CHECK (read-only unless the normal live flow enables approval)
# ============================================================

def connect_to_fieldpo(p):
    context = p.chromium.launch_persistent_context(
        BROWSER_PROFILE_DIR,
        headless=False,
        args=["--start-maximized"],
        no_viewport=True,
    )
    page = context.new_page()
    page.goto(FIELDPO_URL)
    try:
        page.wait_for_url("**/fieldpo/dashboard**", timeout=120000)
        print("Reached FieldPO dashboard.")
    except Exception:
        print("WARNING: Did not detect the dashboard URL within 2 minutes. "
              "Continuing anyway -- check the browser window if something looks wrong.")
    dismiss_attention_popup(page)
    return context, page


def dismiss_attention_popup(page):
    """
    FieldPO occasionally (not always) shows an 'ATTENTION PLEASE!' popup
    about open FPOs. Wait a couple seconds to give it a chance to appear,
    check once, and click 'Resume Work' if it's there. If it's not there,
    proceed normally without waiting any longer.
    """
    page.wait_for_timeout(2000)  # give the popup a couple seconds to appear, if it's coming

    resume_button = page.locator("button:has-text('Resume Work')")
    if resume_button.count() > 0 and resume_button.first.is_visible():
        print("  'ATTENTION PLEASE' popup detected -- clicking Resume Work.")
        resume_button.first.click()
        page.wait_for_timeout(500)
    # else: popup isn't showing, proceed immediately


def go_to_active_work_order_tab(page, vin):
    """
    Navigates from the dashboard to a VIN's Active Work Order tab.
    Does NOT click into a PO -- that's a separate step, since a PO might
    not exist yet.
    TODO: verify every selector below against the real page.
    """
    page.goto(FIELDPO_URL)
    dismiss_attention_popup(page)

    page.get_by_text("Search", exact=True).click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(500)

    page.get_by_placeholder("WO#, PO#, MVA, VIN").fill(vin)
    page.get_by_role("button", name="Search").click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1500)

    mva_locator = page.locator("text=MVA#").first
    mva_text = mva_locator.locator("xpath=..").inner_text()
    mva_match = re.search(r"MVA#\s*:?\s*(\d{8,9})", mva_text, re.IGNORECASE)
    mva = normalize_mva(mva_match.group(1)) if mva_match else ""

    mva_locator.click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)

    page.get_by_text("Active Work Order", exact=False).click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)
    return mva


def click_into_po(page, po_number=None):
    """
    Call only after confirming a PO exists (see po_exists below).
    TODO: verify this selector against the real page.
    """
    if po_number:
        po_link = page.get_by_text(po_number, exact=True)
        count = po_link.count()
        if count != 1:
            raise RuntimeError(f"Expected exactly one PO {po_number}, found {count}")
        po_link.click()
    else:
        page.locator("text=PO#").first.click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)


def po_exists(page, po_number=None):
    """
    No PO shows no clear indication either way on the Active Work Order
    tab -- absence of a "PO#" card is our only signal that none exists.
    """
    if not po_number:
        return page.locator("text=PO#").count() > 0
    count = page.get_by_text(po_number, exact=True).count()
    if count > 1:
        raise RuntimeError(f"Expected at most one PO {po_number}, found {count}")
    return count == 1


def read_po_status_and_amount(page):
    """
    TODO: verify these selectors against the real approval page, and
    confirm the exact wording of the "not yet paid" status badge
    (currently assuming it contains "PENDING" -- update if it's different).
    Returns (status_text, auth_amount_string).
    """
    if page.locator("text=APPROVED").count() > 0:
        status_text = "APPROVED"
    elif page.locator("text=IN PROGRESS").count() > 0:
        status_text = "IN PROGRESS"
    else:
        status_text = "UNKNOWN"

    auth_amount_text = page.locator("text=Authorized Amount").locator("..").inner_text()
    auth_amount = "".join(c for c in auth_amount_text if c.isdigit() or c == ".")

    return status_text, auth_amount


def return_to_fieldpo_home(page):
    """Return to the FieldPO dashboard using the verified top-header Home icon."""
    page.locator("mat-icon[aria-label='home']").click()
    page.wait_for_url("**/fieldpo/dashboard**", timeout=30000)
    dismiss_attention_popup(page)


def append_direct_approval_result(record):
    """Persist one direct-approval result for audit and restart review."""
    with open(DIRECT_APPROVAL_LOG_FILE, "a", encoding="utf-8") as result_log:
        result_log.write(json.dumps(record, sort_keys=True) + "\n")


def _normalize_person_name(value):
    return str(value or "").strip().casefold()


def read_work_order_created_by(page):
    """Read Created By from the active Work Order data section."""
    value = page.locator(
        "div.dataSection > div:has(> span:text-is('Created By:')) > span:nth-child(2)"
    )
    return value.inner_text().strip()


def is_allowed_work_order_creator(created_by_value):
    if not ALLOWED_WORK_ORDER_CREATED_BY:
        return False
    created_key = _normalize_person_name(created_by_value)
    return any(
        created_key == _normalize_person_name(name)
        for name in ALLOWED_WORK_ORDER_CREATED_BY
    )


def finalize_outlook_invoice(row):
    """Move an invoice to the canonical Processed folder after verification."""
    entry_id = str(row.get("_outlook_entry_id", "")).strip()
    if not entry_id:
        raise RuntimeError("Missing Outlook EntryID for verified invoice")
    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    mail = outlook.GetItemFromID(entry_id)
    invoice_folder = get_invoice_folder()
    processed_folder = get_or_create_processed_folder(invoice_folder)
    mail.Move(processed_folder)
    print(
        f"  Outlook email moved to {PROCESSED_FOLDER_NAME}: "
        f"{row.get('subject', '')}"
    )


def step_check(
    max_invoices=None,
    return_home_after_each=False,
    approve_eligible=False,
    confirm_each=False,
    selected_rows=None,
    po_numbers=None,
    target_by_po=None,
    min_age_days=None,
):
    print("\n" + "=" * 60)
    if approve_eligible:
        print("STEP 2: CHECK AND ACT - review each current invoice in FieldPO")
    else:
        print("STEP 2: CHECK - looking up each queued VIN in FieldPO")
    print("=" * 60)

    rows = read_queue()
    new_rows = [r for r in rows if r["status"] == "new"]
    if po_numbers is not None:
        allowed_po_numbers = set(po_numbers)
        new_rows = [
            row for row in new_rows
            if str(row.get("po_number", "")).strip().upper() in allowed_po_numbers
        ]
    if target_by_po is not None:
        rows_by_po = {}
        for row in new_rows:
            po_number = str(row.get("po_number", "")).strip().upper()
            rows_by_po.setdefault(po_number, []).append(row)
        duplicate_po_numbers = {
            po_number
            for po_number, po_rows in rows_by_po.items()
            if po_number and len(po_rows) > 1
        }
        for row in new_rows:
            if str(row.get("po_number", "")).strip().upper() in duplicate_po_numbers:
                row["status"] = "identity_mismatch"
        if duplicate_po_numbers:
            print(
                "Duplicate invoice records found; quarantining PO(s): "
                + ", ".join(sorted(duplicate_po_numbers))
            )
            new_rows = [
                row
                for row in new_rows
                if str(row.get("po_number", "")).strip().upper() not in duplicate_po_numbers
            ]
    if selected_rows is not None:
        selected_entry_ids = {
            _queue_identity_key(
                row.get("subject", ""),
                row.get("vin", ""),
                row.get("invoice_amount", ""),
            ): row.get("_outlook_entry_id", "")
            for row in selected_rows
        }
        selected_keys = {
            _queue_identity_key(
                row.get("subject", ""),
                row.get("vin", ""),
                row.get("invoice_amount", ""),
            )
            for row in selected_rows
        }
        new_rows = [
            row
            for row in new_rows
            if _queue_identity_key(
                row.get("subject", ""),
                row.get("vin", ""),
                row.get("invoice_amount", ""),
            ) in selected_keys
        ]
        for row in new_rows:
            identity_key = _queue_identity_key(
                row.get("subject", ""),
                row.get("vin", ""),
                row.get("invoice_amount", ""),
            )
            row["_outlook_entry_id"] = selected_entry_ids.get(identity_key, "")
    if not new_rows:
        print("No rows with status 'new' to check.")
        return []

    new_rows_sorted = sorted(new_rows, key=lambda r: parse_received_timestamp(r.get("received", "")))
    if max_invoices is not None:
        new_rows_sorted = new_rows_sorted[:max_invoices]

    progress_total = len(new_rows_sorted)
    progress_positions = {
        id(row): index
        for index, row in enumerate(new_rows_sorted, start=1)
    }
    progress_counts = {
        "closed": 0,
        "approved_open": 0,
        "already_approved": 0,
        "skipped": 0,
        "failed": 0,
    }

    def log_progress(row, outcome, detail=""):
        progress_counts[outcome] += 1
        po_number = str(row.get("po_number", "")).strip().upper() or "NO_PO_NUMBER"
        label = {
            "closed": "CLOSED",
            "approved_open": "APPROVED_WO_OPEN",
            "already_approved": "ALREADY_APPROVED",
            "skipped": "SKIPPED",
            "failed": "FAILED",
        }[outcome]
        detail_suffix = f" ({detail})" if detail else ""
        print(
            f"[PROGRESS] {progress_positions[id(row)]}/{progress_total} {po_number} - "
            f"{label}{detail_suffix} | closed={progress_counts['closed']} "
            f"approved_open={progress_counts['approved_open']} "
            f"already_approved={progress_counts['already_approved']} "
            f"skipped={progress_counts['skipped']} failed={progress_counts['failed']}"
        )

    check_results = []
    valid_rows = []
    for row in new_rows_sorted:
        raw_vin = row.get("vin", "")
        if not is_valid_vin(raw_vin):
            print(
                f"Skipping invoice '{row.get('subject', '')}' for this run "
                f"-- invalid VIN: {raw_vin or 'missing'}."
            )
            check_results.append({**row, "check_status": "invalid_vin", "mva": ""})
            log_progress(row, "skipped", "invalid_vin")
            continue
        valid_rows.append(row)

    if not valid_rows:
        print("No valid queued VINs to check.")
        return check_results

    approval_failure = None
    with sync_playwright() as p:
        context, page = connect_to_fieldpo(p)

        for row in valid_rows:
            vin = normalize_vin(row["vin"])
            po_number = str(row.get("po_number", "")).strip().upper()
            print(f"\nChecking VIN {vin} ...")
            try:
                mva = go_to_active_work_order_tab(page, vin)

                if target_by_po is not None:
                    po_number = str(row.get("po_number", "")).strip().upper()
                    target = target_by_po.get(po_number)
                    if target is None:
                        row["status"] = "identity_mismatch"
                        check_results.append({**row, "check_status": "identity_mismatch", "mva": mva})
                        print(f"  No authorization target found for {po_number or 'missing PO'}.")
                        log_progress(row, "skipped", "authorization_target_missing")
                        continue
                    try:
                        validate_invoice_target(target, vin, mva)
                    except LookupError as exc:
                        row["status"] = "identity_mismatch"
                        check_results.append({**row, "check_status": "identity_mismatch", "mva": mva})
                        print(f"  Identity mismatch: {exc}")
                        log_progress(row, "skipped", "identity_mismatch")
                        continue

                found_po = po_exists(page, po_number) if po_number else po_exists(page)
                if not found_po:
                    row["status"] = "no_po"
                    check_results.append({**row, "check_status": "no_po", "mva": mva})
                    expected = f" {po_number}" if po_number else ""
                    print(f"  No PO{expected} found for this VIN -- flagging for manual review.")
                    log_progress(row, "skipped", "po_not_found")
                    continue

                work_order_created_by = read_work_order_created_by(page)
                if po_number:
                    click_into_po(page, po_number)
                else:
                    click_into_po(page)
                status_text, auth_amount = read_po_status_and_amount(page)

                if status_text == "APPROVED":
                    row["status"] = "already_paid"
                    check_results.append({
                        **row,
                        "check_status": "already_paid",
                        "mva": mva,
                        "work_order_created_by": work_order_created_by,
                    })
                    print("  FieldPO status: already APPROVED. No approval action needed.")
                    finalize_outlook_invoice(row)
                    print("  Continuing to next invoice.")
                    log_progress(row, "already_approved")
                    continue
                if status_text != "IN PROGRESS":
                    row["status"] = "error"
                    check_results.append({
                        **row,
                        "check_status": "unexpected_po_status",
                        "mva": mva,
                        "work_order_created_by": work_order_created_by,
                    })
                    print(
                        f"  FieldPO status is {status_text}; approval requires IN PROGRESS."
                    )
                    log_progress(row, "skipped", f"status_{status_text.lower().replace(' ', '_')}")
                    continue

                invoice_amount = row["invoice_amount"]
                match = abs(float(auth_amount) - float(invoice_amount)) < 0.01
                row["auth_amount"] = auth_amount
                row["match"] = str(match)
                row["status"] = "checked"
                check_result = {
                    **row,
                    "check_status": "checked",
                    "mva": mva,
                    "work_order_created_by": work_order_created_by,
                }
                check_results.append(check_result)
                print(f"  Invoice: ${invoice_amount}  |  Auth: ${auth_amount}  |  Match: {match}")

                if approve_eligible:
                    if min_age_days is None:
                        strict_result = evaluate_checked_invoice(check_result)
                    else:
                        strict_result = evaluate_checked_invoice(
                            check_result,
                            min_age_days=min_age_days,
                        )
                    if strict_result["reasons"]:
                        print(
                            "  Manual review required: "
                            + ", ".join(strict_result["reasons"])
                        )
                        log_progress(row, "skipped", ",".join(strict_result["reasons"]))
                        continue
                    if strict_result.get("excluded_price_sku") and not match:
                        print(
                            f"  Excluded SKU {strict_result['invoice_sku']}: "
                            "invoice-controlled price exception accepted."
                        )
                    try:
                        approval_kwargs = {"request_permission": confirm_each}
                        if po_number:
                            approval_kwargs["expected_po_number"] = po_number
                        if target_by_po is not None:
                            approval_kwargs["allow_open_if_closure_unavailable"] = True
                        approved = approve_displayed_po(page, vin, **approval_kwargs)
                    except Exception as exc:
                        row["status"] = "approve_failed"
                        approval_failure = RuntimeError(
                            f"Approval run stopped after failure for VIN {vin}: {exc}"
                        )
                        print(f"  Failed to approve and close: {exc}")
                        log_progress(row, "failed", "approval_confirmation_failed")
                        break
                    if approved is None:
                        print("  Skipped by user. No approval or closure action was performed.")
                        log_progress(row, "skipped", "user_declined")
                        continue
                    row["status"] = "approved"
                    finalize_outlook_invoice(row)
                    if approved:
                        print("  Approved and closed.")
                        log_progress(row, "closed")
                    else:
                        print("  Approved; Work Order remains open.")
                        log_progress(row, "approved_open")

            except Exception as e:
                print(f"  Could not complete check for VIN {vin}: {type(e).__name__}: {e}")
                current_url = "unknown"
                current_title = "unknown"
                try:
                    current_url = page.url
                except Exception:
                    pass
                try:
                    current_title = page.title()
                except Exception:
                    pass
                print(f"    URL: {current_url}")
                print(f"    Title: {current_title}")
                row["status"] = "error"
                check_results.append({**row, "check_status": "error", "mva": ""})
                log_progress(row, "failed", type(e).__name__)
            finally:
                if (return_home_after_each or approve_eligible) and approval_failure is None:
                    try:
                        return_to_fieldpo_home(page)
                    except Exception as exc:
                        print(f"  Could not return to FieldPO Home: {type(exc).__name__}: {exc}")

        context.close()

    write_queue(rows)
    print(f"\nQueue updated: {QUEUE_CSV}")
    processed_count = sum(progress_counts.values())
    print(
        f"[PROGRESS] FINAL {processed_count}/{progress_total} | "
        f"closed={progress_counts['closed']} "
        f"approved_open={progress_counts['approved_open']} "
        f"already_approved={progress_counts['already_approved']} "
        f"skipped={progress_counts['skipped']} failed={progress_counts['failed']}"
    )
    if approval_failure is not None:
        raise approval_failure
    return check_results


def append_closure_decision(record):
    """Append one structured dry-run closure decision."""
    with open(DECISION_LOG_FILE, "a", encoding="utf-8") as decision_log:
        decision_log.write(json.dumps(record, sort_keys=True) + "\n")


def evaluate_checked_invoice(result, min_age_days=None):
    """Apply the shared dry-run and live approval gates to one checked invoice."""
    invoice_date = None
    extracted_vin = None
    extracted_amount = None
    invoice_sku = None
    parse_error = ""
    try:
        invoice_date, extracted_vin, extracted_amount = extract_invoice_data(
            result.get("pdf_path", "")
        )
    except (OSError, ValueError) as exc:
        parse_error = type(exc).__name__
    try:
        invoice_sku = extract_invoice_sku(result.get("pdf_path", ""))
    except (OSError, ValueError):
        invoice_sku = None

    excluded_price_sku = invoice_sku in EXCLUDED_STANDARD_PRICE_SKUS

    check_status = result.get("check_status", "")
    if not check_status and result.get("status") == "checked":
        check_status = "checked"
    reasons = []
    if check_status != "checked":
        reasons.append(check_status or "fieldpo_check_incomplete")
    if parse_error:
        reasons.append("invoice_pdf_unavailable")
    if extracted_vin and normalize_vin(extracted_vin) != normalize_vin(result.get("vin", "")):
        reasons.append("invoice_vin_mismatch")
    if extracted_amount and extracted_amount != result.get("invoice_amount", ""):
        reasons.append("invoice_amount_mismatch")

    evaluation = evaluate_closure(
        invoice_date=invoice_date,
        invoice_amount=extracted_amount or result.get("invoice_amount", ""),
        auth_amount=result.get("auth_amount", ""),
        mva=result.get("mva", ""),
        work_order_created_by=result.get("work_order_created_by", ""),
        min_age_days=min_age_days,
        allow_price_mismatch=excluded_price_sku,
    )
    reasons.extend(reason for reason in evaluation["reasons"] if reason not in reasons)
    return {
        "invoice_date": invoice_date,
        "invoice_amount": extracted_amount or result.get("invoice_amount", ""),
        "invoice_sku": invoice_sku,
        "excluded_price_sku": excluded_price_sku,
        "evaluation": evaluation,
        "reasons": reasons,
    }


def step_dry_run_review(check_results, min_age_days=None):
    """Evaluate checked invoices and log decisions without pressing Approve."""
    print("\n" + "=" * 60)
    print("STEP 3: DRY RUN - review only; APPROVE will not be pressed")
    print("=" * 60)
    decisions = []

    for result in check_results:
        strict_result = evaluate_checked_invoice(result, min_age_days=min_age_days)
        invoice_date = strict_result["invoice_date"]
        evaluation = strict_result["evaluation"]
        reasons = strict_result["reasons"]
        decision = "WOULD_CLOSE" if not reasons else "SKIPPED"
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": "dry_run",
            "subject": result.get("subject", ""),
            "vin": result.get("vin", ""),
            "mva": evaluation["mva"],
            "invoice_date": invoice_date.isoformat() if invoice_date else "",
            "invoice_age_days": evaluation["invoice_age_days"],
            "invoice_amount": strict_result["invoice_amount"],
            "fieldpo_amount": result.get("auth_amount", ""),
            "work_order_created_by": result.get("work_order_created_by", ""),
            "price_match": evaluation["price_match"],
            "decision": decision,
            "reasons": reasons,
        }
        append_closure_decision(record)
        decisions.append(record)
        reason_text = ", ".join(reasons) if reasons else "all closure requirements met"
        print(
            f"  {decision}: VIN {record['vin']} | MVA {record['mva'] or 'UNKNOWN'} "
            f"| {reason_text}"
        )

    if not decisions:
        print("No invoices were available for dry-run review.")
    print("\nDRY RUN COMPLETE: no Approve or Close action was performed.")
    return decisions


# ============================================================
# STEP 3: APPROVE (review list, one confirm, then click APPROVE)
# ============================================================

def approve_displayed_po(
    page,
    vin,
    request_permission=True,
    expected_po_number=None,
    close_work_order=True,
    allow_open_if_closure_unavailable=False,
):
    if request_permission:
        confirm = input(
            f"\nFieldPO is displaying VIN {vin}. "
            f"Type 'yes' to approve the PO"
            f"{' and close the Work Order' if close_work_order else ''}: "
        ).strip().lower()
        if confirm != "yes":
            return None

    page.get_by_role("button", name="Approve", exact=True).click()
    page.wait_for_timeout(1000)

    close_wo_checkbox = page.locator("div.checkboxSection").filter(
        has_text=re.compile(r"^\s*Close Work Order\s*$")
    ).locator("input.checkboxInput[type='checkbox']")
    checkbox_count = close_wo_checkbox.count()
    effective_close_work_order = close_work_order
    if close_work_order and checkbox_count == 0 and allow_open_if_closure_unavailable:
        effective_close_work_order = False
        print("  FieldPO did not offer Work Order closure; approving PO only.")
    if effective_close_work_order:
        if checkbox_count != 1:
            raise RuntimeError(
                f"Expected exactly one Close Work Order checkbox, found {checkbox_count}. "
                "Approval was not confirmed."
            )
        if not close_wo_checkbox.is_checked():
            close_wo_checkbox.check()
        if not close_wo_checkbox.is_checked():
            raise RuntimeError(
                "Close Work Order checkbox did not remain checked. Approval was not confirmed."
            )
    elif checkbox_count > 1:
        raise RuntimeError(
            f"Expected at most one Close Work Order checkbox, found {checkbox_count}. "
            "Approval was not confirmed."
        )
    elif checkbox_count == 1 and close_wo_checkbox.is_checked():
        close_wo_checkbox.uncheck()
        if close_wo_checkbox.is_checked():
            raise RuntimeError(
                "Close Work Order checkbox remained checked. Approval was not confirmed."
            )

    page.get_by_role("button", name="Confirm", exact=True).click()
    success_message = page.locator("div.successMsg")
    success_message.wait_for(state="visible", timeout=30000)
    success_count = success_message.count()
    if success_count != 1:
        raise RuntimeError(
            f"Expected exactly one approval result message, found {success_count}."
        )
    success_text = " ".join(success_message.inner_text().split())
    po_match = re.search(r"\bPO#\s+(FPO\d+)\s+approved\b", success_text, re.IGNORECASE)
    if not po_match:
        raise RuntimeError(f"PO approval was not confirmed: {success_text}")
    approved_po_number = po_match.group(1).upper()
    if expected_po_number and approved_po_number != expected_po_number.upper():
        raise RuntimeError(
            f"Approved PO {approved_po_number}, expected {expected_po_number.upper()}"
        )
    closure_confirmed = re.search(r"\bWO#\s+\S+\s+is closed\.", success_text, re.IGNORECASE)
    if effective_close_work_order and not closure_confirmed:
        raise RuntimeError(f"Work Order closure was not confirmed: {success_text}")
    if not effective_close_work_order and closure_confirmed:
        raise RuntimeError(f"Work Order closed during PO-only approval: {success_text}")
    return effective_close_work_order


def step_direct_approve(targets, max_invoices=None, start_index=0):
    """Approve exact authorized PO targets without requiring Outlook invoices."""
    remaining_targets = targets[start_index:]
    selected_targets = (
        remaining_targets[:max_invoices]
        if max_invoices is not None
        else remaining_targets
    )
    print("\n" + "=" * 60)
    print("DIRECT APPROVAL - exact MVA, VIN, and PO authorization list")
    print("=" * 60)
    print(
        f"Processing {len(selected_targets)} authorized PO target(s) "
        f"starting at row {start_index + 1} of {len(targets)}."
    )

    results = []
    approval_failure = None
    final_target_index_by_mva = {
        target["mva"]: index
        for index, target in enumerate(targets)
    }
    with sync_playwright() as playwright:
        context, page = connect_to_fieldpo(playwright)
        for selected_index, target in enumerate(selected_targets, start=start_index):
            mva = target["mva"]
            vin = target["vin"]
            po_number = target["po_number"]
            close_work_order = final_target_index_by_mva[mva] == selected_index
            print(f"\nApproving {po_number} | MVA {mva} | VIN {vin} ...")
            result = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                **target,
                "result": "",
                "detail": "",
            }
            try:
                found_mva = go_to_active_work_order_tab(page, vin)
                if found_mva != mva:
                    raise LookupError(f"MVA mismatch: found {found_mva or 'none'}, expected {mva}")
                work_order_created_by = read_work_order_created_by(page)
                result["work_order_created_by"] = work_order_created_by
                if not is_allowed_work_order_creator(work_order_created_by):
                    raise LookupError(
                        f"Work Order Created By {work_order_created_by or 'missing'} is not allowed"
                    )
                if not po_exists(page, po_number):
                    raise LookupError(f"Exact PO {po_number} not found for MVA {mva}")
                click_into_po(page, po_number)
                status_text, _ = read_po_status_and_amount(page)
                if status_text == "APPROVED":
                    result["result"] = "already_approved"
                    print("  Already APPROVED; no action needed.")
                elif status_text != "IN PROGRESS":
                    raise LookupError(f"PO status is {status_text}; approval not attempted")
                else:
                    work_order_closed = approve_displayed_po(
                        page,
                        vin,
                        request_permission=False,
                        expected_po_number=po_number,
                        close_work_order=close_work_order,
                        allow_open_if_closure_unavailable=True,
                    )
                    if work_order_closed:
                        result["result"] = "approved_closed"
                        print("  Approved PO and closed Work Order.")
                    else:
                        result["result"] = "approved_open"
                        print("  Approved PO; Work Order remains open for another listed PO.")
            except LookupError as exc:
                result["result"] = "skipped"
                result["detail"] = str(exc)
                print(f"  Skipped: {exc}")
            except Exception as exc:
                result["result"] = "failed"
                result["detail"] = str(exc)
                approval_failure = RuntimeError(
                    f"Direct approval stopped at {po_number}: {exc}"
                )
                print(f"  FAILED: {exc}")
            finally:
                append_direct_approval_result(result)
                results.append(result)

            if approval_failure is not None:
                break
            try:
                return_to_fieldpo_home(page)
            except Exception as exc:
                approval_failure = RuntimeError(
                    f"Direct approval stopped after {po_number}; could not return Home: {exc}"
                )
                break
        context.close()

    if approval_failure is not None:
        raise approval_failure
    approved_count = sum(result["result"] == "approved_closed" for result in results)
    approved_open_count = sum(result["result"] == "approved_open" for result in results)
    already_count = sum(result["result"] == "already_approved" for result in results)
    skipped_count = sum(result["result"] == "skipped" for result in results)
    print(
        f"\nDirect approval complete: approved/closed={approved_count}, "
        f"approved/open={approved_open_count}, "
        f"already approved={already_count}, skipped={skipped_count}"
    )
    return results


def approve_one_vin(page, invoice_row, request_permission=True):
    vin = invoice_row["vin"]
    po_number = str(invoice_row.get("po_number", "")).strip().upper()
    mva = go_to_active_work_order_tab(page, vin)
    work_order_created_by = read_work_order_created_by(page)
    strict_result = evaluate_checked_invoice({
        **invoice_row,
        "check_status": "checked",
        "mva": mva,
        "work_order_created_by": work_order_created_by,
    })
    if strict_result["reasons"]:
        return False, work_order_created_by, strict_result["reasons"]

    if po_number:
        click_into_po(page, po_number)
    else:
        click_into_po(page)
    status_text, _ = read_po_status_and_amount(page)
    if status_text == "APPROVED":
        return False, work_order_created_by, ["already_approved"]
    if status_text != "IN PROGRESS":
        return False, work_order_created_by, ["unexpected_po_status"]
    approval_kwargs = {"request_permission": request_permission}
    if po_number:
        approval_kwargs["expected_po_number"] = po_number
    approved = approve_displayed_po(page, vin, **approval_kwargs)
    if approved is None:
        return None, work_order_created_by, ["user_declined"]
    return True, work_order_created_by, []


def step_approve(confirm_each=False, max_invoices=None, po_numbers=None):
    print("\n" + "=" * 60)
    print("STEP 3: APPROVE - review and confirm")
    print("=" * 60)

    rows = read_queue()
    checked_rows = [r for r in rows if r["status"] == "checked"]
    if po_numbers is not None:
        allowed_po_numbers = set(po_numbers)
        checked_rows = [
            row for row in checked_rows
            if str(row.get("po_number", "")).strip().upper() in allowed_po_numbers
        ]

    matches = [r for r in checked_rows if r["match"] == "True"]
    matches.sort(key=lambda r: parse_received_timestamp(r.get("received", "")))

    approval_limit = MAX_APPROVALS_PER_RUN if max_invoices is None else max_invoices
    if approval_limit is not None and len(matches) > approval_limit:
        print(f"NOTE: {len(matches)} matched invoices found, but the approval limit "
              f"is {approval_limit}. Only the first {approval_limit} will "
              "be shown/approved this run. Re-run to process the rest.")
        matches = matches[:approval_limit]

    mismatches = [
        r for r in rows
        if r["status"] == "checked" and r["match"] == "False"
    ]
    errors = [r for r in rows if r["status"] == "error"]
    already_paid = [r for r in rows if r["status"] == "already_paid"]
    no_po = [r for r in rows if r["status"] == "no_po"]

    if already_paid:
        print(f"Already paid, skipped automatically ({len(already_paid)}):")
        for r in already_paid:
            print(f"  VIN {r['vin']}  |  {r['subject']}")

    if no_po:
        print(f"\nNO PO FOUND -- needs manual review/creation ({len(no_po)}):")
        for r in no_po:
            print(f"  VIN {r['vin']}  |  {r['subject']}")

    if not matches and not mismatches and not errors:
        print("Nothing pending review.")
        return

    print(f"\nPRICE-MATCHED CANDIDATES ({len(matches)}) -- strict gates checked in FieldPO:")
    for r in matches:
        print(f"  VIN {r['vin']}  |  ${r['invoice_amount']}  |  {r['subject']}")

    if mismatches:
        print(f"\nNEEDS MANUAL REVIEW ({len(mismatches)}) -- amounts did NOT match:")
        for r in mismatches:
            print(f"  VIN {r['vin']}  |  invoice ${r['invoice_amount']} vs auth ${r['auth_amount']}  |  {r['subject']}")

    if errors:
        print(f"\nCOULD NOT BE CHECKED ({len(errors)}):")
        for r in errors:
            print(f"  VIN {r['vin']}  |  {r['subject']}")

    if ALLOWED_WORK_ORDER_CREATED_BY:
        print(
            "\nApproval gate: Work Order Created By must be one of: "
            + ", ".join(ALLOWED_WORK_ORDER_CREATED_BY)
        )

    if not matches:
        print("\nNo matched invoices to approve right now.")
        return

    if confirm_each:
        print("\nEach invoice will remain visible in FieldPO while approval permission is requested.")
    else:
        print(f"\nProduction mode: processing {len(matches)} strictly eligible invoice(s).")

    approval_failure = None
    with sync_playwright() as p:
        context, page = connect_to_fieldpo(p)

        for row in rows:
            if row not in matches:
                continue
            vin = row["vin"]
            print(f"\nApproving VIN {vin} ...")
            try:
                should_approve, work_order_created_by, rejection_reasons = approve_one_vin(
                    page,
                    row,
                    request_permission=confirm_each,
                )
                if should_approve is None:
                    print("  Skipped by user. No approval or closure action was performed.")
                    continue
                if not should_approve:
                    if rejection_reasons == ["already_approved"]:
                        row["status"] = "already_paid"
                        finalize_outlook_invoice(row)
                        print("  FieldPO status: already APPROVED. No approval action needed.")
                        continue
                    print(
                        "  Skipped by strict closure gates: "
                        + ", ".join(rejection_reasons)
                    )
                    continue
                row["status"] = "approved"
                print("  Approved.")
            except Exception as e:
                print(f"  Failed to approve: {e}")
                row["status"] = "approve_failed"
                approval_failure = RuntimeError(
                    f"Approval run stopped after failure for VIN {vin}: {e}"
                )
                break

        context.close()

    write_queue(rows)
    if approval_failure is not None:
        raise approval_failure
    print("\nDone. Queue updated.")


# ============================================================
# MAIN
# ============================================================

def setup_logging():
    """
    Sends everything printed to the console into run_log.txt as well,
    with a timestamped header marking the start of this run. The log
    file accumulates across runs -- nothing gets overwritten.
    """
    os.makedirs(BASE_DIR, exist_ok=True)
    log_file_handle = open(LOG_FILE, "a", encoding="utf-8")
    log_file_handle.write(f"\n{'=' * 70}\n")
    log_file_handle.write(f"RUN STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_file_handle.write(f"{'=' * 70}\n")
    sys.stdout = Tee(sys.stdout, log_file_handle)
    sys.stderr = Tee(sys.stderr, log_file_handle)


def main():
    setup_logging()
    print(
        f"Loaded {len(EXCLUDED_STANDARD_PRICE_SKUS)} excluded standard-price SKU(s) "
        f"from {CONFIG_FILE}"
    )

    parser = argparse.ArgumentParser(description="AGN invoice automation - all-in-one")
    parser.add_argument("--setup-credentials", action="store_true",
                         help="Store your FieldPO username/password securely and exit.")
    parser.add_argument("--extract-only", action="store_true", help="Run only the extract step.")
    parser.add_argument("--check-only", action="store_true", help="Run only the check step.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract, check, and log closure decisions without pressing Approve.",
    )
    parser.add_argument("--approve", dest="approve", action="store_true",
                        help="Run only the approve step.")
    parser.add_argument("--approve-only", dest="approve", action="store_true",
                        help="Alias for --approve.")
    parser.add_argument("--silent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--confirm-each",
        action="store_true",
        help="Development mode: request permission before each eligible approval.",
    )
    parser.add_argument("--yes", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-invoices",
        type=int,
        default=None,
        help="Cap invoices processed by each selected stage for this run.",
    )
    parser.add_argument(
        "--min-age-days",
        "--min_age",
        dest="min_age_days",
        type=int,
        default=None,
        help=(
            "Enable a minimum invoice age gate for this run. "
            "When omitted, invoice age is ignored."
        ),
    )
    parser.add_argument(
        "--po-file",
        help="Process only exact FPO numbers listed one per line in this file.",
    )
    parser.add_argument(
        "--direct-approve-file",
        help=(
            "Approve and close exact FieldPO targets from a tab-separated "
            "MVA_NUMBER, VIN_NO, PO_NUMBER file without requiring an invoice."
        ),
    )
    parser.add_argument(
        "--invoice-target-file",
        help=(
            "Require invoices matching a tab-separated MVA_NUMBER, VIN_NO, "
            "PO_NUMBER authorization file."
        ),
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Zero-based starting row for --direct-approve-file continuation.",
    )
    parser.add_argument(
        "--trace-vin",
        action="append",
        help=(
            "Trace one or more VINs across queue history and Outlook invoice emails. "
            "Can be repeated and can include comma-separated VINs."
        ),
    )
    args = parser.parse_args()

    if args.max_invoices is not None and args.max_invoices <= 0:
        parser.error("--max-invoices must be greater than 0")
    if args.min_age_days is not None and args.min_age_days < 0:
        parser.error("--min-age-days must be 0 or greater")
    if args.start_index < 0:
        parser.error("--start-index must be 0 or greater")
    if args.start_index and not args.direct_approve_file:
        parser.error("--start-index requires --direct-approve-file")
    if args.direct_approve_file and (
        args.po_file or args.dry_run or args.extract_only or args.check_only or args.approve
    ):
        parser.error("--direct-approve-file cannot be combined with invoice workflow options")
    if args.invoice_target_file and (args.po_file or args.direct_approve_file or args.approve):
        parser.error(
            "--invoice-target-file cannot be combined with --po-file, "
            "--direct-approve-file, or --approve"
        )

    if args.direct_approve_file:
        parser.error(
            "--direct-approve-file is disabled because every PO approval now requires "
            "a matching invoice"
        )

    po_numbers = None
    target_by_po = None
    if args.invoice_target_file:
        try:
            invoice_targets = load_direct_approval_targets(args.invoice_target_file)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        target_by_po = {target["po_number"]: target for target in invoice_targets}
        po_numbers = list(target_by_po)
        print(
            f"Loaded {len(invoice_targets)} invoice-backed authorization target(s) "
            f"from {args.invoice_target_file}"
        )
    if args.po_file:
        try:
            po_numbers = load_po_numbers(args.po_file)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Loaded {len(po_numbers)} PO number(s) from {args.po_file}")
    po_filter_kwargs = {"po_numbers": po_numbers} if po_numbers is not None else {}
    if target_by_po is not None:
        po_filter_kwargs["target_by_po"] = target_by_po
    age_gate_kwargs = (
        {"min_age_days": args.min_age_days}
        if args.min_age_days is not None
        else {}
    )

    if args.setup_credentials:
        set_fieldpo_credentials()
        return

    trace_vins, invalid_vins = parse_trace_vins(args.trace_vin)
    if invalid_vins:
        print("WARNING: Ignoring invalid VIN values: " + ", ".join(invalid_vins))
    if args.trace_vin:
        if not trace_vins:
            print("No valid VINs were provided. Provide at least one 17-character VIN.")
            return
        step_trace_vins(trace_vins)
        print("\nAll done.")
        return

    if args.dry_run:
        extracted_rows = step_extract(max_invoices=args.max_invoices, **po_filter_kwargs)
        check_results = step_check(
            max_invoices=args.max_invoices,
            return_home_after_each=True,
            selected_rows=extracted_rows,
            **age_gate_kwargs,
            **po_filter_kwargs,
        )
        step_dry_run_review(check_results, **age_gate_kwargs)
    elif args.extract_only:
        step_extract(max_invoices=args.max_invoices, **po_filter_kwargs)
    elif args.check_only:
        step_check(
            max_invoices=args.max_invoices,
            **age_gate_kwargs,
            **po_filter_kwargs,
        )
    elif args.approve:
        step_approve(
            confirm_each=args.confirm_each,
            max_invoices=args.max_invoices,
            **po_filter_kwargs,
        )
    else:
        extracted_rows = step_extract(max_invoices=args.max_invoices, **po_filter_kwargs)
        step_check(
            max_invoices=args.max_invoices,
            approve_eligible=True,
            confirm_each=args.confirm_each,
            selected_rows=extracted_rows,
            **age_gate_kwargs,
            **po_filter_kwargs,
        )

    print("\nAll done.")


if __name__ == "__main__":
    _exit_code = 0
    try:
        main()
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
        _exit_code = 1
    if _exit_code:
        sys.exit(_exit_code)