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
from datetime import datetime
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
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")
CONFIG_FILE = os.path.join(BASE_DIR, "agn_invoices_config.json")

LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")


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


RUNTIME_CONFIG = _load_runtime_config()

ACCOUNT_NAME = str(RUNTIME_CONFIG.get("account_name", "Dirk.Steele@avisbudget.com"))

PROCESSED_FOLDER_NAME = str(RUNTIME_CONFIG.get("processed_folder_name", "Processed"))
PROCESSED_CATEGORY_NAME = str(RUNTIME_CONFIG.get("processed_category_name", "Green Category"))

# If True, email is also moved to Inbox\AGN\Invoice\Processed after extraction.
# Category marking is always applied and is the primary processed signal.
MOVE_TO_PROCESSED_FOLDER = bool(RUNTIME_CONFIG.get("move_to_processed_folder", False))

# Safety limit for the EXTRACT step. None = no limit.
# Keep this small until you trust the output, then raise it or set to None.
MAX_ITEMS_PER_RUN = _int_or_default(RUNTIME_CONFIG.get("max_items_per_run", 3), 3)

# Safety limit for the APPROVE step. None = no limit.
# Keep this at 1 for your first real approval, then raise it once confirmed working.
MAX_APPROVALS_PER_RUN = _int_or_default(RUNTIME_CONFIG.get("max_approvals_per_run", 1), 1)

# If an invoice is this old (or older), allow approval even if amount match is False.
AGE_APPROVAL_DAYS = _int_or_default(RUNTIME_CONFIG.get("age_approval_days", 21), 21)

# Only approve when the page's Created By field matches one of these names.
# Leave empty to disable the Created By approval gate.
_allowed_created_by_raw = RUNTIME_CONFIG.get("allowed_created_by", [])
if isinstance(_allowed_created_by_raw, str):
    _allowed_created_by_raw = [_allowed_created_by_raw]
ALLOWED_CREATED_BY = [str(v).strip() for v in _allowed_created_by_raw if str(v).strip()]

# Backward compatibility: legacy single-name setting.
if not ALLOWED_CREATED_BY:
    _required_created_by = str(RUNTIME_CONFIG.get("required_created_by", "")).strip()
    if _required_created_by:
        ALLOWED_CREATED_BY = [_required_created_by]

FIELDPO_URL = "https://supply-chain.east.prod.sdp.abg.cloud/fieldpo/dashboard"

CREDENTIAL_SERVICE_NAME = "AGN_Automation_FieldPO"

QUEUE_FIELDS = [
    "subject", "received", "vin", "invoice_amount", "pdf_path",
    "status", "auth_amount", "match",
]

QUEUE_STATUS_BUCKETS = {
    "approved": {"approved"},
    "failed": {"approve_failed", "error"},
    "skipped": {"already_paid", "no_po", "creator_mismatch"},
    "processed": {"new", "checked"},
}


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


def mark_processed_category(mail):
    categories = _split_categories(getattr(mail, "Categories", ""))
    if any(PROCESSED_CATEGORY_NAME.lower() == category.lower() for category in categories):
        return
    categories.append(PROCESSED_CATEGORY_NAME)
    mail.Categories = ", ".join(categories)
    mail.Save()


def extract_vin_and_amount(pdf_path):
    """
    Based on the sample invoice format:
        VIN                1FMJK1M83SEA26775
        ...
        Subtotal           $340.00
    """
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    vin_match = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", full_text)
    vin = vin_match.group(1) if vin_match else None

    amount_match = re.search(r"Subtotal\s*\$?([\d,]+\.\d{2})", full_text)
    amount = amount_match.group(1).replace(",", "") if amount_match else None

    return vin, amount


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
        writer.writerows(rows)


def write_queue(rows):
    if not rows:
        return
    with open(QUEUE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def step_extract():
    print("\n" + "=" * 60)
    print("STEP 1: EXTRACT - reading Outlook AGN\\Invoice folder")
    print("=" * 60)

    os.makedirs(ATTACHMENT_DIR, exist_ok=True)
    os.makedirs(BASE_DIR, exist_ok=True)

    invoice_folder = get_invoice_folder()
    processed_folder = get_or_create_processed_folder(invoice_folder) if MOVE_TO_PROCESSED_FOLDER else None

    items = invoice_folder.Items
    items.Sort("[ReceivedTime]", False)  # oldest first
    all_items = list(items)  # copy before moving anything

    existing_rows = read_queue()
    existing_keys = {
        _queue_identity_key(r.get("subject", ""), r.get("vin", ""), r.get("invoice_amount", ""))
        for r in existing_rows
    }

    rows = []
    for mail in all_items:
        if MAX_ITEMS_PER_RUN is not None and len(rows) >= MAX_ITEMS_PER_RUN:
            print(f"Reached MAX_ITEMS_PER_RUN limit ({MAX_ITEMS_PER_RUN}). Stopping for this run.")
            break
        try:
            if mail.Class != 43:  # olMail only
                continue

            if has_processed_category(mail):
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

            identity_key = _queue_identity_key(mail.Subject, vin, amount)
            if identity_key in existing_keys:
                mark_processed_category(mail)
                if MOVE_TO_PROCESSED_FOLDER:
                    mail.Move(processed_folder)
                print(f"Skipping duplicate queue key for '{mail.Subject}' -> VIN {vin}, ${amount}")
                continue

            queue_row = {
                "subject": mail.Subject,
                "received": str(mail.ReceivedTime),
                "vin": vin,
                "invoice_amount": amount,
                "pdf_path": pdf_path,
                "status": "new",
                "auth_amount": "",
                "match": "",
            }

            mark_processed_category(mail)
            if MOVE_TO_PROCESSED_FOLDER:
                mail.Move(processed_folder)

            rows.append(queue_row)
            existing_keys.add(identity_key)
            print(f"Processed: {mail.Subject} -> VIN {vin}, ${amount} (categorized: {PROCESSED_CATEGORY_NAME})")

        except Exception as e:
            print(f"ERROR processing an email: {e}")

    if not rows:
        print("No new invoices found.")
        return

    append_to_queue(rows)
    print(f"\n{len(rows)} invoice(s) added to queue: {QUEUE_CSV}")


# ============================================================
# STEP 2: CHECK (FieldPO - read only, no approving)
# ============================================================

def connect_to_fieldpo(p):
    context = p.chromium.launch_persistent_context(BROWSER_PROFILE_DIR, headless=False)
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

    page.locator("text=MVA#").first.click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)

    page.get_by_text("Active Work Order", exact=False).click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)


def click_into_po(page):
    """
    Call only after confirming a PO exists (see po_exists below).
    TODO: verify this selector against the real page.
    """
    page.locator("text=PO#").first.click()
    dismiss_attention_popup(page)
    page.wait_for_timeout(1000)


def po_exists(page):
    """
    No PO shows no clear indication either way on the Active Work Order
    tab -- absence of a "PO#" card is our only signal that none exists.
    """
    return page.locator("text=PO#").count() > 0


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


def _normalize_person_name(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def read_created_by(page):
    """Best-effort extract of Created By value from the active work order page."""
    body_text = page.locator("body").inner_text()
    match = re.search(r"Created\s*By\s*:\s*([^\r\n]+)", body_text, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def should_approve_for_creator(created_by_value):
    if not ALLOWED_CREATED_BY:
        return True
    created_key = _normalize_person_name(created_by_value)
    return any(created_key == _normalize_person_name(name) for name in ALLOWED_CREATED_BY)


def step_check():
    print("\n" + "=" * 60)
    print("STEP 2: CHECK - looking up each queued VIN in FieldPO")
    print("=" * 60)

    rows = read_queue()
    new_rows = [r for r in rows if r["status"] == "new"]
    if not new_rows:
        print("No rows with status 'new' to check.")
        return

    new_rows_sorted = sorted(new_rows, key=lambda r: parse_received_timestamp(r.get("received", "")))

    with sync_playwright() as p:
        context, page = connect_to_fieldpo(p)

        for row in new_rows_sorted:
            vin = row["vin"]
            print(f"\nChecking VIN {vin} ...")
            try:
                go_to_active_work_order_tab(page, vin)

                if not po_exists(page):
                    row["status"] = "no_po"
                    print("  No PO found for this VIN -- flagging for manual review.")
                    continue

                click_into_po(page)
                status_text, auth_amount = read_po_status_and_amount(page)

                if status_text == "APPROVED":
                    row["status"] = "already_paid"
                    print(f"  Already APPROVED -- skipping, moving to next invoice.")
                    continue

                invoice_amount = row["invoice_amount"]
                match = abs(float(auth_amount) - float(invoice_amount)) < 0.01
                row["auth_amount"] = auth_amount
                row["match"] = str(match)
                row["status"] = "checked"
                print(f"  Invoice: ${invoice_amount}  |  Auth: ${auth_amount}  |  Match: {match}")

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

        context.close()

    write_queue(rows)
    print(f"\nQueue updated: {QUEUE_CSV}")


# ============================================================
# STEP 3: APPROVE (review list, one confirm, then click APPROVE)
# ============================================================

def approve_one_vin(page, vin):
    go_to_active_work_order_tab(page, vin)
    created_by_value = read_created_by(page)
    if not should_approve_for_creator(created_by_value):
        return False, created_by_value

    click_into_po(page)

    # TODO: verify this in practice against the real page
    page.get_by_role("button", name="Approve").click()
    page.wait_for_timeout(1000)

    # A confirmation modal appears: "Approve PO" with details, two checkboxes
    # (Notify Supplier - checked by default, Close Work Order - unchecked by
    # default), and a Confirm button. We check Close Work Order, leave
    # Notify Supplier as-is, then click Confirm.
    # TODO: verify these selectors against the real modal.
    close_wo_checkbox = page.get_by_text("Close Work Order", exact=False).locator("xpath=preceding-sibling::input")
    if close_wo_checkbox.count() == 0:
        # fallback: some checkbox implementations put the input as a sibling differently
        close_wo_checkbox = page.locator("label:has-text('Close Work Order') input")
    if close_wo_checkbox.count() > 0 and not close_wo_checkbox.first.is_checked():
        close_wo_checkbox.first.check()

    page.get_by_role("button", name="Confirm").click()
    page.wait_for_timeout(1000)
    return True, created_by_value


def step_approve(auto_confirm=False):
    print("\n" + "=" * 60)
    print("STEP 3: APPROVE - review and confirm")
    print("=" * 60)

    rows = read_queue()
    now = datetime.now()
    checked_rows = [r for r in rows if r["status"] == "checked"]

    strict_matches = [r for r in checked_rows if r["match"] == "True"]
    age_override_candidates = []
    for row in checked_rows:
        if row["match"] == "True":
            continue
        received_at = parse_received_timestamp(row.get("received", ""))
        age_days = (now - received_at).days
        if age_days >= AGE_APPROVAL_DAYS:
            age_override_candidates.append(row)

    matches = strict_matches + age_override_candidates
    matches.sort(key=lambda r: parse_received_timestamp(r.get("received", "")))

    if MAX_APPROVALS_PER_RUN is not None and len(matches) > MAX_APPROVALS_PER_RUN:
        print(f"NOTE: {len(matches)} matched invoices found, but MAX_APPROVALS_PER_RUN "
              f"is {MAX_APPROVALS_PER_RUN}. Only the first {MAX_APPROVALS_PER_RUN} will "
              "be shown/approved this run. Re-run to process the rest.")
        matches = matches[:MAX_APPROVALS_PER_RUN]

    mismatches = [
        r for r in rows
        if r["status"] == "checked" and r["match"] == "False" and r not in age_override_candidates
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

    print(f"\nREADY TO APPROVE ({len(matches)}) -- amounts matched:")
    for r in matches:
        if r in age_override_candidates:
            received_at = parse_received_timestamp(r.get("received", ""))
            age_days = (now - received_at).days
            print(
                f"  VIN {r['vin']}  |  ${r['invoice_amount']}  |  {r['subject']}"
                f"  |  AGE OVERRIDE ({age_days} days old)"
            )
        else:
            print(f"  VIN {r['vin']}  |  ${r['invoice_amount']}  |  {r['subject']}")

    if age_override_candidates:
        print(
            f"\nAGE-BASED APPROVAL ENABLED ({len(age_override_candidates)}) "
            f"-- invoice age >= {AGE_APPROVAL_DAYS} days"
        )

    if mismatches:
        print(f"\nNEEDS MANUAL REVIEW ({len(mismatches)}) -- amounts did NOT match:")
        for r in mismatches:
            print(f"  VIN {r['vin']}  |  invoice ${r['invoice_amount']} vs auth ${r['auth_amount']}  |  {r['subject']}")

    if errors:
        print(f"\nCOULD NOT BE CHECKED ({len(errors)}):")
        for r in errors:
            print(f"  VIN {r['vin']}  |  {r['subject']}")

    if ALLOWED_CREATED_BY:
        print("\nApproval gate: Created By must be one of: " + ", ".join(ALLOWED_CREATED_BY))

    if not matches:
        print("\nNo matched invoices to approve right now.")
        return

    if auto_confirm:
        print(f"\nAuto-confirm enabled. Proceeding to approve {len(matches)} invoice(s).")
    else:
        confirm = input(f"\nType 'yes' to approve all {len(matches)} matched invoice(s) above: ").strip().lower()
        if confirm != "yes":
            print("Cancelled. Nothing was approved.")
            return

    with sync_playwright() as p:
        context, page = connect_to_fieldpo(p)

        for row in rows:
            if row not in matches:
                continue
            vin = row["vin"]
            print(f"\nApproving VIN {vin} ...")
            try:
                should_approve, created_by_value = approve_one_vin(page, vin)
                if not should_approve:
                    row["status"] = "creator_mismatch"
                    print(
                        f"  Skipped. Created By is '{created_by_value or 'UNKNOWN'}' "
                        f"(allowed: {', '.join(ALLOWED_CREATED_BY)})."
                    )
                    continue
                row["status"] = "approved"
                print("  Approved.")
            except Exception as e:
                print(f"  Failed to approve: {e}")
                row["status"] = "approve_failed"

        context.close()

    write_queue(rows)
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

    parser = argparse.ArgumentParser(description="AGN invoice automation - all-in-one")
    parser.add_argument("--setup-credentials", action="store_true",
                         help="Store your FieldPO username/password securely and exit.")
    parser.add_argument("--extract-only", action="store_true", help="Run only the extract step.")
    parser.add_argument("--check-only", action="store_true", help="Run only the check step.")
    parser.add_argument("--approve", dest="approve", action="store_true",
                        help="Run only the approve step.")
    parser.add_argument("--approve-only", dest="approve", action="store_true",
                        help="Alias for --approve.")
    parser.add_argument("--silent", action="store_true",
                        help="Run without waiting for interactive close prompt."
                             " Approval still requires confirmation unless --yes is provided.")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-confirm approval prompt in step 3 (use with caution).")
    parser.add_argument(
        "--trace-vin",
        action="append",
        help=(
            "Trace one or more VINs across queue history and Outlook invoice emails. "
            "Can be repeated and can include comma-separated VINs."
        ),
    )
    args = parser.parse_args()

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

    if args.extract_only:
        step_extract()
    elif args.check_only:
        step_check()
    elif args.approve:
        step_approve(auto_confirm=args.yes)
    else:
        step_extract()
        step_check()
        step_approve(auto_confirm=args.yes)

    print("\nAll done.")


if __name__ == "__main__":
    _silent_mode = False
    try:
        _silent_mode = "--silent" in sys.argv
        main()
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
    finally:
        if not _silent_mode:
            input("\nPress Enter to close...")