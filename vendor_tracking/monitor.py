"""AutoGlassNow vendor tracking monitor.

Standalone runner — independent of the main Phase 1-7 pipeline.

Run via:
    .venv\\Scripts\\python.exe vendor_tracking\\monitor.py
  or:
    Run-VendorTracking.cmd

Workflow:
  1. Connect to Gmail via IMAP.
  2. Search for emails from known AutoGlassNow sender domains
     within the configured lookback window.
  3. Skip any email whose Message-ID is already in the idempotency store.
  4. Classify each email (appointment, approval-needed, technician-assigned).
  5. Parse the email and update the GlassClaims sheet row matched by
     VIN + Arrival Date compound key.
  6. Print a run summary. Approval Needed blockers are prominently flagged.
"""

import email as email_module
import imaplib
import json
import logging
import logging.config
import os
import re
import sys
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ─── Path setup ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from vendor_tracking.email_parser import (
    EmailType,
    AppointmentEmailData,
    ApprovalNeededEmailData,
    TechnicianAssignedEmailData,
    classify_email,
    debug_extract_job_id_from_appointment_html,
    get_html_body,
    parse_appointment_email,
    parse_approval_needed_email,
    parse_technician_assigned_email,
)
from vendor_tracking.idempotency_store import IdempotencyStore
from vendor_tracking.sheet_updater import (
    VendorSheetUpdater,
    STATUS_APPROVAL_NEEDED,
)

# ─── Logging ──────────────────────────────────────────────────────────────────
_LOG_INI = BASE_DIR / "config" / "logging.ini"
if _LOG_INI.exists():
    try:
        logging.config.fileConfig(str(_LOG_INI), disable_existing_loggers=False)
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

log = logging.getLogger("vendor_tracking.monitor")

# ─── Config loading ───────────────────────────────────────────────────────────
_ORCHESTRATOR_CONFIG_PATH = BASE_DIR / "orchestrator_config.json"
_ORCHESTRATOR_PROJECT_CONFIG_PATH = BASE_DIR / "orchestrator_project.json"
_ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_project.local.json"
_ORCHESTRATOR_LOCAL_CONFIG_PATH = BASE_DIR / "orchestrator_config.local.json"


def _load_config() -> dict:
    """Load orchestrator config merging all config layers in the same order as
    GlassOrchestrator.py: base → project → project.local → orchestrator.local.
    Service account path is read from config (service_account_json key).
    """
    def _read(path: Path, required: bool) -> dict:
        if not path.exists():
            if required:
                raise RuntimeError(f"[CONFIG] Missing required file: {path}")
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Config load failed for %s: %s", path, exc)
            return {}

    merged: dict = {}
    for path, required in [
        (_ORCHESTRATOR_CONFIG_PATH, True),
        (_ORCHESTRATOR_PROJECT_CONFIG_PATH, False),
        (_ORCHESTRATOR_PROJECT_LOCAL_CONFIG_PATH, False),
        (_ORCHESTRATOR_LOCAL_CONFIG_PATH, False),
    ]:
        merged.update(_read(path, required))
    return merged


# ─── Run summary ──────────────────────────────────────────────────────────────

@dataclass
class RunSummary:
    """Accumulated results from a single monitor run."""
    total_fetched: int = 0
    skipped_idempotent: int = 0
    skipped_unknown: int = 0
    processed: int = 0
    needs_review: list[str] = field(default_factory=list)
    approval_needed: list[str] = field(default_factory=list)   # VINs waiting on our approval
    errors: list[str] = field(default_factory=list)


# ─── IMAP helpers ─────────────────────────────────────────────────────────────

def _connect_imap(imap_server: str, email_account: str, email_password: str) -> imaplib.IMAP4_SSL:
    """Open an authenticated IMAP connection."""
    mail = imaplib.IMAP4_SSL(imap_server)
    mail.login(email_account, email_password)
    mail.select("inbox")
    return mail


def _search_vendor_emails(
    mail: imaplib.IMAP4_SSL,
    senders: list[str],
    since_date: str,
) -> list[bytes]:
    """Return IMAP message IDs for AutoGlassNow emails since a given IMAP date.

    Searches ALL (not just UNSEEN) — idempotency is handled separately.
    """
    all_ids: list[bytes] = []
    seen: set[bytes] = set()

    for sender_domain in senders:
        criteria = f'(FROM "{sender_domain}" SINCE "{since_date}")'
        status, msg_ids = mail.search(None, criteria)
        if status != "OK" or not msg_ids or not msg_ids[0]:
            continue
        for mid in msg_ids[0].split():
            if mid not in seen:
                seen.add(mid)
                all_ids.append(mid)

    return all_ids


def _fetch_message(mail: imaplib.IMAP4_SSL, imap_id: bytes) -> Optional[email_module.message.Message]:
    """Fetch and parse a single MIME message by IMAP sequence ID."""
    status, msg_data = mail.fetch(imap_id, "(RFC822)")
    if status != "OK" or not msg_data or not msg_data[0]:
        return None
    raw = msg_data[0][1]
    if not isinstance(raw, bytes):
        return None
    return email_module.message_from_bytes(raw)


# ─── Monitor ──────────────────────────────────────────────────────────────────

class VendorTrackingMonitor:
    """Fetch AutoGlassNow emails and update vendor tracking columns in GlassClaims."""

    def __init__(
        self,
        config: dict,
        since_date: str | None = None,
        ignore_idempotency: bool = False,
        decision_log_path: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._config = config

        # IMAP credentials
        self._imap_server = str(config.get("imap_server", "imap.gmail.com"))
        self._email_account = os.getenv("GLASS_EMAIL_ACCOUNT") or str(config.get("email_account", ""))
        self._email_password = os.getenv("GLASS_EMAIL_PASSWORD") or str(config.get("email_password", ""))

        # Vendor tracking config
        self._spreadsheet_id = str(config.get("vendor_tracking_spreadsheet_id", ""))
        self._sheet_name = str(config.get("vendor_tracking_sheet_name", "GlassClaims"))
        self._senders: list[str] = list(config.get("vendor_tracking_senders", ["autoglassnow.com", "omegaedi.com"]))
        self._lookback_days = int(config.get("vendor_tracking_lookback_days", 30))
        self._since_date = since_date or str(config.get("vendor_tracking_since_date", "")).strip()
        self._ignore_idempotency = ignore_idempotency
        self._dry_run = dry_run
        decision_log_rel = decision_log_path or str(
            config.get("vendor_tracking_decision_log", "data/vendor_tracking_decisions.jsonl")
        )
        decision_log_candidate = Path(decision_log_rel)
        self._decision_log_path = (
            decision_log_candidate
            if decision_log_candidate.is_absolute()
            else BASE_DIR / decision_log_candidate
        )

        idempotency_rel = str(config.get("vendor_tracking_idempotency_store", "data/vendor_tracking_processed.json"))
        self._idempotency_path = BASE_DIR / idempotency_rel

        self._store = IdempotencyStore(self._idempotency_path)
        self._updater: Optional[VendorSheetUpdater] = None

    @property
    def _skip_idempotency_gate(self) -> bool:
        """Return True when idempotency skip checks should be bypassed."""
        return self._ignore_idempotency or self._dry_run

    def _resolve_imap_since_date(self) -> str:
        """Resolve the IMAP SINCE date in dd-Mon-YYYY format.

        Priority order:
          1) CLI --since-date value
          2) config vendor_tracking_since_date
          3) computed lookback_days from now
        """
        if self._since_date:
            raw = self._since_date.strip()
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%d-%b-%Y")
                except ValueError:
                    continue
            raise RuntimeError(
                f"Invalid since date '{raw}'. Use MM/DD/YYYY (example: 05/04/2026)."
            )

        return (datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)).strftime("%d-%b-%Y")

    def _record_decision(self, **payload: object) -> None:
        """Append one decision event to JSONL audit log."""
        event = {
            "ts_utc": datetime.now(tz=timezone.utc).isoformat(),
            **payload,
        }
        try:
            self._decision_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._decision_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True) + "\n")
        except OSError as exc:
            log.warning("Decision log write failed (%s): %s", self._decision_log_path, exc)

    def _service_account_path(self) -> Path:
        """Resolve the service account JSON path from config, relative to BASE_DIR."""
        raw = str(self._config.get("service_account_json", "Service_account.json"))
        path = Path(raw)
        return path if path.is_absolute() else BASE_DIR / path

    def _validate_config(self) -> list[str]:
        """Return a list of configuration errors; empty list means config is valid."""
        errors = []
        if not self._email_account:
            errors.append("Email account not configured. Set GLASS_EMAIL_ACCOUNT env var or email_account in config.")
        if not self._email_password:
            errors.append("Email password not configured. Set GLASS_EMAIL_PASSWORD env var or email_password in config.")
        if not self._spreadsheet_id:
            errors.append(
                "vendor_tracking_spreadsheet_id is not set. "
                "Add it to orchestrator_config.local.json pointing to the DEV sheet."
            )
        sa_path = self._service_account_path()
        if not sa_path.exists():
            errors.append(f"Service account JSON not found: {sa_path}")
        return errors

    # ─── Main entry point ────────────────────────────────────────────────────

    def run(self) -> RunSummary:
        """Execute a full vendor tracking monitor run."""
        summary = RunSummary()

        errors = self._validate_config()
        if errors:
            for err in errors:
                log.error("Config error: %s", err)
            summary.errors.extend(errors)
            return summary

        # Connect to sheet
        self._updater = VendorSheetUpdater(
            spreadsheet_id=self._spreadsheet_id,
            sheet_name=self._sheet_name,
            service_account_json=str(self._service_account_path()),
        )
        try:
            self._updater.connect()
            if not self._dry_run:
                self._updater.ensure_columns()
        except Exception as exc:
            error_text = str(exc) or f"{type(exc).__name__}"
            log.error("Failed to connect to Google Sheet: %s", error_text)
            summary.errors.append(error_text)
            return summary

        # Connect to IMAP
        try:
            mail = _connect_imap(self._imap_server, self._email_account, self._email_password)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("IMAP connection failed: %s", exc)
            summary.errors.append(f"IMAP connection failed: {exc}")
            return summary

        try:
            since_date = self._resolve_imap_since_date()
            imap_ids = _search_vendor_emails(mail, self._senders, since_date)
            log.info("Found %d candidate vendor email(s) since %s", len(imap_ids), since_date)
            summary.total_fetched = len(imap_ids)

            for imap_id in imap_ids:
                msg = _fetch_message(mail, imap_id)
                if msg is None:
                    log.warning("Could not fetch IMAP message %s — skipping", imap_id)
                    continue
                self._process_message(msg, summary)
        finally:
            try:
                mail.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

        return summary

    # ─── Per-message processing ──────────────────────────────────────────────

    def _process_message(self, msg: email_module.message.Message, summary: RunSummary) -> None:
        """Classify, parse, and act on a single vendor email."""
        message_id = str(msg.get("Message-ID", "")).strip()
        subject = str(msg.get("Subject", "")).strip()

        if not message_id:
            # Synthesize a pseudo-ID from From + Subject + Date to allow idempotency
            message_id = f"synthetic|{msg.get('From','')}|{subject}|{msg.get('Date','')}"

        if not self._skip_idempotency_gate and self._store.is_processed(message_id):
            log.debug("Skipping already-processed: %s", message_id)
            summary.skipped_idempotent += 1
            self._record_decision(
                message_id=message_id,
                subject=subject,
                decision="skip_idempotent",
            )
            return

        email_type = classify_email(msg)
        log.info("Email type: %-30s | Subject: %s", email_type.name, subject)

        if email_type == EmailType.UNKNOWN:
            summary.skipped_unknown += 1
            if not self._skip_idempotency_gate:
                self._store.mark_processed(message_id)
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=email_type.name,
                decision="skip_unknown",
            )
            return

        html_body = get_html_body(msg)

        try:
            if email_type == EmailType.APPOINTMENT_CONFIRMATION:
                self._handle_appointment(html_body, message_id, subject, summary)
            elif email_type == EmailType.APPROVAL_NEEDED:
                self._handle_approval_needed(html_body, message_id, subject, summary)
            elif email_type == EmailType.TECHNICIAN_ASSIGNED:
                self._handle_technician_assigned(html_body, message_id, subject, summary)
            # COMPLETION_RECEIPT is Phase 4 — log and skip for now
            elif email_type == EmailType.COMPLETION_RECEIPT:
                log.info("Completion receipt email deferred to Phase 4 — skipping: %s", subject)
                if not self._skip_idempotency_gate:
                    self._store.mark_processed(message_id)
                self._record_decision(
                    message_id=message_id,
                    subject=subject,
                    email_type=email_type.name,
                    decision="skip_phase4_deferred",
                )
                return
        except Exception as exc:
            log.error("Error processing message '%s': %s", subject, exc, exc_info=True)
            summary.errors.append(f"{subject}: {exc}")
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=email_type.name,
                decision="error",
                detail=str(exc),
            )
            return

        if not self._skip_idempotency_gate:
            self._store.mark_processed(message_id)
        summary.processed += 1

    # ─── Appointment confirmation ─────────────────────────────────────────────

    def _handle_appointment(
        self,
        html: str,
        message_id: str,
        subject: str,
        summary: RunSummary,
    ) -> None:
        data: AppointmentEmailData = parse_appointment_email(html)
        debug_job_id, debug_meta = debug_extract_job_id_from_appointment_html(html)
        log.info(
            "Appointment — JobId: %s, Date: %s, Vehicle: %s",
            data.job_id, data.appointment_date, data.vehicle,
        )

        # Appointment confirmation emails don't carry a VIN — they seed identifiers.
        # New template often omits job-tracker URL but includes appointment reference.
        if not data.job_id and data.appointment_ref:
            log.info(
                "Seeded appointment reference %s (no JobId in template) — row update deferred until VIN is available",
                data.appointment_ref,
            )
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPOINTMENT_CONFIRMATION.name,
                decision="seed_appointment_ref",
                appointment_ref=data.appointment_ref,
                appointment_date=data.appointment_date,
                vehicle=data.vehicle or "",
                debug_reason=debug_meta.get("reason", "unknown"),
                href_preview=debug_meta.get("href_preview", ""),
            )
            return

        # Legacy template path where no seed identifier can be extracted.
        if not data.job_id:
            log.warning("Appointment email has no extractable JobId — manual review needed: %s", subject)
            summary.needs_review.append(f"[No JobId] {subject}")
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPOINTMENT_CONFIRMATION.name,
                decision="needs_review_no_job_id",
                debug_reason=debug_meta.get("reason", "unknown"),
                href_preview=debug_meta.get("href_preview", ""),
                debug_job_id=debug_job_id or "",
            )
            return

        # Log the seeded job for visibility; no auto row update without VIN.
        log.info(
            "Seeded JobId %s (tracker: %s) — row update deferred until VIN is available",
            data.job_id, data.tracker_url,
        )
        self._record_decision(
            message_id=message_id,
            subject=subject,
            email_type=EmailType.APPOINTMENT_CONFIRMATION.name,
            decision="seed_job_id",
            job_id=data.job_id,
            tracker_url=data.tracker_url,
            appointment_date=data.appointment_date,
        )

    # ─── Approval-needed ─────────────────────────────────────────────────────

    def _handle_approval_needed(
        self,
        html: str,
        message_id: str,
        subject: str,
        summary: RunSummary,
    ) -> None:
        data: ApprovalNeededEmailData = parse_approval_needed_email(html)
        log.info("Approval needed — VIN: %s, Cost: %s, ETA: %s", data.vin, data.quoted_cost, data.eta_notes)

        if not data.vin:
            log.warning("Approval-needed email has no parseable VIN — manual review needed: %s", subject)
            summary.needs_review.append(f"[No VIN] {subject}")
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPROVAL_NEEDED.name,
                decision="needs_review_no_vin",
            )
            return

        assert self._updater is not None
        if self._updater.has_unique_resolved_vin(data.vin):
            log.info(
                "Skipping approval-needed email for VIN %s — unique sheet row is already resolved",
                data.vin,
            )
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPROVAL_NEEDED.name,
                decision="skip_row_resolved",
                vin=data.vin,
            )
            return

        # Approval-needed emails don't carry an Arrival Date directly.
        # We need to match by VIN and the date contained in the email or current date.
        # For the initial implementation, attempt match by VIN only (write_needs_review
        # handles ambiguous cases), then escalate to full compound-key match once
        # the operator confirms Arrival Date.
        #
        # Best-effort: try to parse a date from the email received date or the email body.
        received_date = str(msg_date_from_html_or_now(html))

        match = self._updater.find_row(data.vin, received_date)

        fields: dict = {
            "Repair Status": STATUS_APPROVAL_NEEDED,
            "Approval Needed": "Yes",
        }
        if data.quoted_cost:
            fields["Cost"] = data.quoted_cost
        if data.eta_notes:
            fields["Repair Status Notes"] = f"ETA: {data.eta_notes}"
        if data.work_order_ref:
            fields["Vendor Job Number"] = data.work_order_ref

        if match.is_ok:
            if self._updater.is_row_resolved(match.row_index):  # type: ignore[arg-type]
                log.info(
                    "Skipping approval update for VIN %s — row %d already resolved",
                    data.vin,
                    match.row_index,
                )
                self._record_decision(
                    message_id=message_id,
                    subject=subject,
                    email_type=EmailType.APPROVAL_NEEDED.name,
                    decision="skip_row_resolved",
                    vin=data.vin,
                    row_index=match.row_index,
                )
                return

            if self._dry_run:
                summary.approval_needed.append(data.vin)
                self._record_decision(
                    message_id=message_id,
                    subject=subject,
                    email_type=EmailType.APPROVAL_NEEDED.name,
                    decision="dry_run_would_update_row",
                    vin=data.vin,
                    row_index=match.row_index,
                    fields=fields,
                )
                return

            self._updater.update_vendor_fields(match.row_index, fields)  # type: ignore[arg-type]
            summary.approval_needed.append(data.vin)
            log.warning(">>> APPROVAL NEEDED — VIN %s (row %d) <<<", data.vin, match.row_index)
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPROVAL_NEEDED.name,
                decision="updated_row",
                vin=data.vin,
                row_index=match.row_index,
                fields=fields,
            )
        else:
            log.warning(
                "Approval-needed match failed (%s): %s — attempting VIN-only fallback",
                match.status, match.note,
            )
            summary.needs_review.append(f"[{match.status}] VIN={data.vin} | {match.note}")
            # Fallback: try VIN-only write for visibility
            if self._dry_run:
                self._record_decision(
                    message_id=message_id,
                    subject=subject,
                    email_type=EmailType.APPROVAL_NEEDED.name,
                    decision="dry_run_would_write_needs_review",
                    vin=data.vin,
                    match_status=match.status,
                    match_note=match.note,
                )
            else:
                self._updater.write_needs_review(
                    data.vin,
                    f"Approval needed (auto match failed: {match.note})",
                )
            # Still surface in approval_needed list so operator sees the blocker
            summary.approval_needed.append(f"{data.vin} (needs review)")
            self._record_decision(
                message_id=message_id,
                subject=subject,
                email_type=EmailType.APPROVAL_NEEDED.name,
                decision="needs_review_match_failure",
                vin=data.vin,
                match_status=match.status,
                match_note=match.note,
            )

    # ─── Technician assigned ─────────────────────────────────────────────────

    def _handle_technician_assigned(
        self,
        html: str,
        message_id: str,
        subject: str,
        summary: RunSummary,
    ) -> None:
        data: TechnicianAssignedEmailData = parse_technician_assigned_email(html)
        log.info("Technician assigned — Date: %s, Tracker: %s", data.assigned_date, data.tracker_url)
        # Technician-assigned emails do not carry a VIN.
        # Provisional: log the event; manual update path during interim.
        log.info("Technician-assigned notice received — manual row update required (no VIN in this email type): %s", subject)
        self._record_decision(
            message_id=message_id,
            subject=subject,
            email_type=EmailType.TECHNICIAN_ASSIGNED.name,
            decision="manual_update_required",
            assigned_date=data.assigned_date,
            tracker_url=data.tracker_url,
        )


# ─── Date helper ──────────────────────────────────────────────────────────────

def msg_date_from_html_or_now(html: str) -> str:
    """Extract the first date from HTML body or return today's date as YYYY-MM-DD."""
    date_pattern = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
    m = date_pattern.search(html)
    if m:
        return m.group(1)
    return datetime.now(tz=timezone.utc).strftime("%m/%d/%Y")


# ─── Summary output ───────────────────────────────────────────────────────────

def _print_summary(summary: RunSummary) -> None:
    """Print a human-readable run summary to stdout."""
    print()
    print("=" * 60)
    print("  VENDOR TRACKING MONITOR — RUN SUMMARY")
    print("=" * 60)
    print(f"  Emails fetched    : {summary.total_fetched}")
    print(f"  Already processed : {summary.skipped_idempotent}")
    print(f"  Unknown / skipped : {summary.skipped_unknown}")
    print(f"  Processed         : {summary.processed}")
    print(f"  Errors            : {len(summary.errors)}")

    if summary.approval_needed:
        print()
        print("  !! APPROVAL NEEDED — ACTION REQUIRED !!")
        print("  " + "-" * 40)
        for vin in summary.approval_needed:
            print(f"  >>> VIN: {vin}")
        print("  " + "-" * 40)
        print("  Vendor cannot proceed until we respond.")

    if summary.needs_review:
        print()
        print("  Needs Manual Review:")
        for item in summary.needs_review:
            print(f"    - {item}")

    if summary.errors:
        print()
        print("  Errors:")
        for err in summary.errors:
            print(f"    - {err}")

    print("=" * 60)
    print()


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="AutoGlassNow vendor tracking monitor")
    parser.add_argument(
        "--since-date",
        help="Process vendor emails since this date (MM/DD/YYYY), e.g. 05/04/2026",
    )
    parser.add_argument(
        "--ignore-idempotency",
        action="store_true",
        help="Reprocess emails even if their Message-ID already exists in the idempotency store",
    )
    parser.add_argument(
        "--dry-run",
        "--Dry_run",
        dest="dry_run",
        action="store_true",
        help="Simulate conclusions without writing to the sheet or idempotency store",
    )
    parser.add_argument(
        "--decision-log",
        help="Path to JSONL decision log (default: data/vendor_tracking_decisions.jsonl)",
    )
    args = parser.parse_args()

    config = _load_config()
    monitor = VendorTrackingMonitor(
        config,
        since_date=args.since_date,
        ignore_idempotency=args.ignore_idempotency,
        decision_log_path=args.decision_log,
        dry_run=args.dry_run,
    )
    summary = monitor.run()
    _print_summary(summary)
    # Exit non-zero if there are unresolved errors
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
