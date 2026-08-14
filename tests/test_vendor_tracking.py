"""
Phase 3 Vendor Tracking Tests

VT-1:  extract_job_id_from_zeta_href — Zeta base64 decode
VT-2:  parse_appointment_email — body field extraction
VT-3:  parse_approval_needed_email — VIN, cost, ETA extraction
VT-4:  parse_technician_assigned_email — assigned date extraction
VT-5:  VendorSheetUpdater.find_row — match, not-found, ambiguous
VT-6:  VendorSheetUpdater.update_vendor_fields — targeted column write
VT-7:  Integration — mocked IMAP + mocked worksheet end-to-end
VT-8:  IdempotencyStore — no duplicate writes on rerun
VT-9:  Approval Needed blocker surfaced in run summary
"""

import base64
import email as email_module
import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

import vendor_tracking.email_parser as email_parser
from vendor_tracking.email_parser import (
    AppointmentEmailData,
    ApprovalNeededEmailData,
    CompletionReceiptEmailData,
    EmailType,
    TechnicianAssignedEmailData,
    _find_view_status_href,
    classify_email,
    extract_job_id_from_zeta_href,
    normalize_vin,
    parse_appointment_email,
    parse_approval_needed_email,
    parse_completion_receipt_email,
    parse_technician_assigned_email,
)
from vendor_tracking.idempotency_store import IdempotencyStore
from vendor_tracking.sheet_updater import (
    VendorSheetUpdater,
    STATUS_APPROVAL_NEEDED,
    STATUS_COMPLETED,
    STATUS_SCHEDULED,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_zeta_segment(url: str) -> str:
    """Build a Zeta path segment for a given decoded URL (prefix='V')."""
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"V{encoded}"


SAMPLE_JOB_ID = "4723644"
SAMPLE_TRACKER_URL = f"https://www.autoglassnow.com/job-tracker/{SAMPLE_JOB_ID}/"

SAMPLE_ZETA_HREF = (
    "https://e.e.autoglassnow.com/click"
    "/Xdummy1"
    f"/{_make_zeta_segment(SAMPLE_TRACKER_URL)}"
    "/Xdummy2"
)

REAL_QP_APPOINTMENT_HREF_SNIPPET = """
<a href=
=3D"https://e.e.autoglassnow.com/click?EZGlyay5hdmlzQGdtYWlsLmNvbQ/CeyJtaWQ=
iOiIxNzc3OTgwNjgzMDA2YjYxNDVmZjkxY2VmIiwiY3QiOiJhdXRvLWdsYXNzLW5vdy1wcm9kLT=
gxYjI2OTU5YzUyZDczYTZhMmE1MTVmODY4NjUxYmJkLTAiLCJyZCI6ImdtYWlsLmNvbSJ9/VaHR=
0cHM6Ly93d3cuYXV0b2dsYXNzbm93LmNvbS9qb2ItdHJhY2tlci80NzIzNjQ0/SWkhfYXV0b2ds=
YXNzX0ROVEFOMDUwNTIwMjZjMTk0NTYxNg/LZGQ1/qP3V0bV9jYW1wYWlnbj1BR05fQVVUT19BU=
FBPSU5UTUVOVFJFUVVFU1RFRF9QSDZfTk9PRkZFUl9ORVdfMDBfVjNfQkFVJnV0bV9tZWRpdW09=
ZW1haWwmdXRtX3NvdXJjZT16ZXRhJmJ0X3VzZXJfaWQ9YzZmd3BlTnB6Z0p6bW11JTJCMUx6Q21=
ERXJKRlNJQk5vUjJGaSUyRnd6NFFmd1ZaZ2xRcGZ2WnB3bTZyZ1BtJTJGM0g1SkE4YnFoNnZ3aE=
xGTEdjSVJWa0c5MVhvNm5IZzNVZm1mekNHekhlYiUyRlZ1dnRBNWM0MDRkZXByOUZQcHRVaCUyQ=
kN3JmJ0X3RzPTE3Nzc5ODA2ODMwMDc/gafnVGg/JMDUwNTIwMjZDMTk0NTYxNg/sdo93186fe8"=
 style=3D"border-collapse: collapse; mso-line-height-rule: exactly; text-de=
coration: none;" data-location=3D"t5"><img src=3D"https://images.e.autoglas=
snow.com/images/5faeaf6f9bb9a940e2aef81a3117cf46/a1f2bfd0421734229161004910=
e6f824.jpg" width=3D"300" alt=3D"VIEW STATUS" border=3D"0"></a>
"""

MALFORMED_DECODED_APPOINTMENT_HREF_SNIPPET = """
<a href=
="https://e.e.autoglassnow.com/click?x/y/VaHR0cHM6Ly93d3cuYXV0b2dsYXNzbm93LmNvbS9qb2ItdHJhY2tlci80NzIzNjQ0/z"
 style="text-decoration:none;"><img alt="VIEW STATUS"></a>
"""

APPOINTMENT_HTML = f"""
<html><body>
<p>Thank you for scheduling your service!</p>
<p>Date: 05/06/2026</p>
<p>Service: Windshield Replacement</p>
<p>Vehicle: 2026 Gmc Terrain</p>
<p>Location: 123 Main St, Atlanta GA</p>
<a href="{SAMPLE_ZETA_HREF}">VIEW STATUS</a>
</body></html>
"""

APPROVAL_HTML = """
<html><body>
<p>Please Advise — Prior Approval Required</p>
<table>
<tr><td>VIN</td><td>1HGBH41JXMN109186</td></tr>
<tr><td>Part</td><td>OEM Windshield</td></tr>
<tr><td>Total</td><td>$450.00</td></tr>
<tr><td>ETA</td><td>3-5 business days</td></tr>
<tr><td>Quote #</td><td>WO99887</td></tr>
</table>
</body></html>
"""

TECH_ASSIGNED_HTML = f"""
<html><body>
<p>Your Certified Technician Has Been Assigned</p>
<p>Assigned Date: 05/07/2026</p>
<a href="{SAMPLE_ZETA_HREF}">VIEW STATUS</a>
</body></html>
"""

COMPLETION_HTML = """
<html><body>
<p>Thank you for choosing Auto Glass Now.</p>
<table>
<tr><td>VIN</td><td>1HGBH41JXMN109186</td></tr>
<tr><td>Total</td><td>$306.00</td></tr>
</table>
</body></html>
"""


def _make_message(
    sender: str = "info@e.autoglassnow.com",
    subject: str = "Test",
    body: str = "",
    message_id: str = "<test@autoglassnow.com>",
) -> email_module.message.Message:
    """Build a minimal email.message.Message for testing."""
    msg = email_module.message_from_string(
        f"From: {sender}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    )
    return msg


def _make_sheet_updater_with_mock(
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[VendorSheetUpdater, MagicMock]:
    """Create a VendorSheetUpdater with a mocked gspread worksheet."""
    all_values = [headers] + data_rows

    mock_ws = MagicMock()
    mock_ws.get_all_values.return_value = all_values

    updater = VendorSheetUpdater.__new__(VendorSheetUpdater)
    updater._spreadsheet_id = "fake_id"
    updater._sheet_name = "GlassClaims"
    updater._service_account_json = "fake.json"
    updater._ws = mock_ws
    updater._headers = headers[:]
    updater._all_values = all_values

    return updater, mock_ws


# ─── VT-1: Zeta base64 decode → JobId ────────────────────────────────────────

class TestVT1_JobIdExtraction:
    """VT-1: extract_job_id_from_zeta_href — Zeta base64 decode"""

    def test_extracts_job_id_from_valid_href(self):
        job_id = extract_job_id_from_zeta_href(SAMPLE_ZETA_HREF)
        assert job_id == SAMPLE_JOB_ID

    def test_returns_none_for_empty_href(self):
        assert extract_job_id_from_zeta_href("") is None

    def test_returns_none_for_non_zeta_href(self):
        assert extract_job_id_from_zeta_href("https://www.example.com/page") is None

    def test_handles_different_job_id(self):
        tracker_url = "https://www.autoglassnow.com/job-tracker/9999999/"
        href = f"https://e.e.autoglassnow.com/click/{_make_zeta_segment(tracker_url)}"
        assert extract_job_id_from_zeta_href(href) == "9999999"

    def test_confirmed_sample_4689437(self):
        tracker_url = "https://www.autoglassnow.com/job-tracker/4689437/"
        href = f"https://e.e.autoglassnow.com/click/A/{_make_zeta_segment(tracker_url)}/B"
        assert extract_job_id_from_zeta_href(href) == "4689437"

    def test_extracts_job_id_from_real_quoted_printable_href(self):
        href = _find_view_status_href(REAL_QP_APPOINTMENT_HREF_SNIPPET)
        assert href is not None
        assert extract_job_id_from_zeta_href(href) == SAMPLE_JOB_ID

    def test_extracts_job_id_from_malformed_decoded_href_attribute(self):
        href = _find_view_status_href(MALFORMED_DECODED_APPOINTMENT_HREF_SNIPPET)
        assert href is not None
        assert extract_job_id_from_zeta_href(href) == SAMPLE_JOB_ID

    def test_extracts_job_id_when_v_segment_contains_wrapping_noise(self):
        noisy_href = (
            "https://e.e.autoglassnow.com/click/A/"
            "VaHR0cHM6Ly93d3cuYXV0b2dsYXNz\n"
            " bm93LmNvbS9qb2ItdHJhY2tlci80NzIzNjQ0/S"
        )
        assert extract_job_id_from_zeta_href(noisy_href) == SAMPLE_JOB_ID


# ─── VT-2: Appointment email body field extraction ────────────────────────────

class TestVT2_AppointmentParsing:
    """VT-2: parse_appointment_email — body field extraction"""

    def setup_method(self):
        self.result: AppointmentEmailData = parse_appointment_email(APPOINTMENT_HTML)

    def test_extracts_job_id(self):
        assert self.result.job_id == SAMPLE_JOB_ID

    def test_extracts_tracker_url(self):
        assert self.result.tracker_url == SAMPLE_TRACKER_URL

    def test_extracts_appointment_date(self):
        assert self.result.appointment_date == "05/06/2026"

    def test_extracts_service_type(self):
        assert self.result.service_type is not None
        assert "Windshield" in self.result.service_type

    def test_extracts_vehicle(self):
        assert self.result.vehicle is not None
        assert "Terrain" in self.result.vehicle

    def test_missing_view_status_returns_none_job_id(self):
        html = "<html><body><p>Thank you for scheduling</p></body></html>"
        result = parse_appointment_email(html)
        assert result.job_id is None
        assert result.tracker_url is None

    def test_falls_back_to_redirect_resolution_when_v_segment_has_no_job_id(self, monkeypatch):
        href = "https://e.e.autoglassnow.com/click/A/VaHR0cHM6Ly93d3cuYXV0b2dsYXNzbm93LmNvbS8/B"
        html = f"<html><body><a href=\"{href}\">VIEW STATUS</a></body></html>"

        monkeypatch.setattr(email_parser, "_extract_job_id_via_redirect", lambda _: "4723644")

        result = parse_appointment_email(html)
        assert result.job_id == "4723644"
        assert result.tracker_url == "https://www.autoglassnow.com/job-tracker/4723644/"

    def test_extracts_appointment_ref_when_job_id_is_unavailable(self, monkeypatch):
        href = (
            "https://e.e.autoglassnow.com/click/A/B/C/"
            "VaHR0cHM6Ly93d3cuYXV0b2dsYXNzbm93LmNvbS8/"
            "SZH_autoglass_DNTAN05052026c1945616/"
            "JMDUwNTIwMjZDMTk0NTYxNg"
        )
        html = f"<html><body><a href=\"{href}\">VIEW STATUS</a></body></html>"

        monkeypatch.setattr(email_parser, "_extract_job_id_via_redirect", lambda _: None)

        result = parse_appointment_email(html)
        assert result.job_id is None
        assert result.tracker_url is None
        assert result.appointment_ref == "05052026C1945616"


class TestCompletionReceiptParsing:

    def test_extracts_job_vin_and_total(self):
        result: CompletionReceiptEmailData = parse_completion_receipt_email(
            "[External] Receipt for Job #4946751",
            COMPLETION_HTML,
        )

        assert result.job_id == "4946751"
        assert result.vin == "1HGBH41JXMN109186"
        assert result.total == "$306.00"

    def test_missing_fields_remain_none(self):
        result = parse_completion_receipt_email("Glass Receipt", "<p>Completed</p>")

        assert result.job_id is None
        assert result.vin is None
        assert result.total is None


# ─── VT-3: Approval-needed email parsing ─────────────────────────────────────

class TestVT3_ApprovalNeededParsing:
    """VT-3: parse_approval_needed_email — VIN, cost, ETA extraction"""

    def setup_method(self):
        self.result: ApprovalNeededEmailData = parse_approval_needed_email(APPROVAL_HTML)

    def test_extracts_vin(self):
        assert self.result.vin == "1HGBH41JXMN109186"

    def test_extracts_cost(self):
        assert self.result.quoted_cost is not None
        assert "450" in self.result.quoted_cost

    def test_extracts_eta(self):
        assert self.result.eta_notes is not None
        assert "business days" in self.result.eta_notes.lower()

    def test_extracts_work_order_ref(self):
        assert self.result.work_order_ref == "WO99887"

    def test_vin_normalization_strips_whitespace(self):
        assert normalize_vin("  1HGBH41JXMN109186  ") == "1HGBH41JXMN109186"

    def test_vin_normalization_rejects_short_vin(self):
        assert normalize_vin("1HGBH41") == ""

    def test_missing_vin_returns_none(self):
        html = "<html><body><p>Please Advise</p><p>Total: $200.00</p></body></html>"
        result = parse_approval_needed_email(html)
        assert result.vin is None


# ─── VT-4: Technician-assigned parsing ───────────────────────────────────────

class TestVT4_TechnicianAssignedParsing:
    """VT-4: parse_technician_assigned_email — assigned date extraction"""

    def test_extracts_assigned_date(self):
        result: TechnicianAssignedEmailData = parse_technician_assigned_email(TECH_ASSIGNED_HTML)
        assert result.assigned_date == "05/07/2026"

    def test_extracts_tracker_url(self):
        result = parse_technician_assigned_email(TECH_ASSIGNED_HTML)
        assert result.tracker_url == SAMPLE_TRACKER_URL

    def test_missing_date_returns_none(self):
        html = "<html><body><p>Your technician has been assigned.</p></body></html>"
        result = parse_technician_assigned_email(html)
        assert result.assigned_date is None


# ─── VT-5: Sheet row matching ────────────────────────────────────────────────

class TestVT5_SheetRowMatching:
    """VT-5: VendorSheetUpdater.find_row — match, not-found, ambiguous"""

    def setup_method(self):
        headers = ["Inventory Date", "MVA", "FPO#", "VIN", "Make"]
        data_rows = [
            ["05/01/2026", "12345678", "FPO001", "1HGBH41JXMN109186", "Honda"],
            ["05/02/2026", "87654321", "FPO002", "2HGBH41JXMN109187", "Toyota"],
            # Duplicate: same VIN + same date as row 1 (to test ambiguous case)
            ["05/01/2026", "99999999", "FPO003", "1HGBH41JXMN109186", "Honda"],
        ]
        self.updater, _ = _make_sheet_updater_with_mock(headers, data_rows)

    def test_finds_unique_match(self):
        result = self.updater.find_row("2HGBH41JXMN109187", "05/02/2026")
        assert result.is_ok
        assert result.row_index == 3  # header=1, first data=2, second=3

    def test_returns_not_found_for_unknown_vin(self):
        result = self.updater.find_row("9ZZZZ00000ZZZ0000", "05/01/2026")
        assert result.status == "not_found"

    def test_returns_ambiguous_for_duplicate_key(self):
        result = self.updater.find_row("1HGBH41JXMN109186", "05/01/2026")
        assert result.status == "ambiguous"

    def test_unique_vin_match_returns_row_and_fields(self):
        headers = ["Inventory Date", "MVA", "VIN", "Damage Type"]
        updater, _ = _make_sheet_updater_with_mock(
            headers,
            [["05/06/2026", "12345678", "1HGBH41JXMN109186", "Replacement"]],
        )

        result = updater.find_unique_row_by_vin("1HGBH41JXMN109186")

        assert result.is_ok
        assert result.row_index == 2
        assert updater.get_row_fields(2, ["MVA", "Damage Type"]) == {
            "MVA": "12345678",
            "Damage Type": "Replacement",
        }

    def test_duplicate_vin_is_ambiguous(self):
        headers = ["Inventory Date", "MVA", "VIN"]
        updater, _ = _make_sheet_updater_with_mock(
            headers,
            [
                ["05/06/2026", "12345678", "1HGBH41JXMN109186"],
                ["06/06/2026", "87654321", "1HGBH41JXMN109186"],
            ],
        )

        result = updater.find_unique_row_by_vin("1HGBH41JXMN109186")

        assert result.status == "ambiguous"
        assert result.row_index is None

    def test_invalid_vin_returns_not_found(self):
        result = self.updater.find_row("TOOSHORT", "05/01/2026")
        assert result.status == "not_found"

    def test_date_normalization_iso_format(self):
        """Sheet date in ISO format should match incoming M/D/YYYY format."""
        headers = ["Inventory Date", "VIN"]
        data_rows = [["2026-05-02", "2HGBH41JXMN109187"]]
        updater, _ = _make_sheet_updater_with_mock(headers, data_rows)
        result = updater.find_row("2HGBH41JXMN109187", "05/02/2026")
        assert result.is_ok


# ─── VT-6: Targeted column write ─────────────────────────────────────────────

class TestVT6_UpdateVendorFields:
    """VT-6: VendorSheetUpdater.update_vendor_fields — targeted column write"""

    def setup_method(self):
        headers = [
            "Inventory Date", "MVA", "VIN",
            "Repair Status", "Approval Needed", "Cost", "Repair Status Notes",
        ]
        data_rows = [
            ["05/01/2026", "12345678", "1HGBH41JXMN109186", "", "", "", ""],
        ]
        self.updater, self.mock_ws = _make_sheet_updater_with_mock(headers, data_rows)
        # Refresh cache reflects empty status so precedence checks work
        self.updater._all_values = [headers] + data_rows

    def test_writes_repair_status(self):
        self.updater.update_vendor_fields(2, {"Repair Status": STATUS_APPROVAL_NEEDED})
        self.mock_ws.batch_update.assert_called_once_with([
            {"range": "D2", "values": [[STATUS_APPROVAL_NEEDED]]},
        ])

    def test_status_precedence_blocks_downgrade(self):
        """Completed status must not be overwritten by a lower-precedence status."""
        headers = [
            "Inventory Date", "MVA", "VIN",
            "Repair Status", "Approval Needed",
        ]
        data_rows = [
            ["05/01/2026", "12345678", "1HGBH41JXMN109186", STATUS_COMPLETED, "No"],
        ]
        updater, mock_ws = _make_sheet_updater_with_mock(headers, data_rows)

        updater.update_vendor_fields(2, {"Repair Status": STATUS_SCHEDULED})
        mock_ws.batch_update.assert_not_called()

    def test_writes_multiple_fields(self):
        self.updater.update_vendor_fields(2, {
            "Approval Needed": "Yes",
            "Cost": "$450.00",
            "Repair Status Notes": "ETA: 5 days",
        })
        self.mock_ws.batch_update.assert_called_once_with([
            {"range": "E2", "values": [["Yes"]]},
            {"range": "F2", "values": [["$450.00"]]},
            {"range": "G2", "values": [["ETA: 5 days"]]},
        ])

    def test_updates_local_cache_without_rereading_sheet(self):
        self.updater.update_vendor_fields(2, {"Repair Status": STATUS_COMPLETED})

        self.mock_ws.get_all_values.assert_not_called()
        assert self.updater.is_row_resolved(2) is True

    def test_skips_unknown_column(self):
        """update_vendor_fields must not raise for unknown column names."""
        self.updater.update_vendor_fields(2, {"NonExistentColumn": "value"})
        self.mock_ws.batch_update.assert_not_called()

    def test_is_row_resolved_true_for_completed(self):
        headers = ["Inventory Date", "MVA", "VIN", "Repair Status"]
        data_rows = [["05/01/2026", "12345678", "1HGBH41JXMN109186", STATUS_COMPLETED]]
        updater, _ = _make_sheet_updater_with_mock(headers, data_rows)
        assert updater.is_row_resolved(2) is True

    def test_is_row_resolved_false_for_approval_needed(self):
        headers = ["Inventory Date", "MVA", "VIN", "Repair Status"]
        data_rows = [["05/01/2026", "12345678", "1HGBH41JXMN109186", STATUS_APPROVAL_NEEDED]]
        updater, _ = _make_sheet_updater_with_mock(headers, data_rows)
        assert updater.is_row_resolved(2) is False

    def test_has_unique_resolved_vin_true(self):
        headers = ["Inventory Date", "MVA", "VIN", "Repair Status"]
        data_rows = [["05/01/2026", "12345678", "1HGBH41JXMN109186", STATUS_COMPLETED]]
        updater, _ = _make_sheet_updater_with_mock(headers, data_rows)
        assert updater.has_unique_resolved_vin("1HGBH41JXMN109186") is True

    def test_has_unique_resolved_vin_false_when_duplicate_vin(self):
        headers = ["Inventory Date", "MVA", "VIN", "Repair Status"]
        data_rows = [
            ["05/01/2026", "12345678", "1HGBH41JXMN109186", STATUS_COMPLETED],
            ["05/02/2026", "87654321", "1HGBH41JXMN109186", STATUS_COMPLETED],
        ]
        updater, _ = _make_sheet_updater_with_mock(headers, data_rows)
        assert updater.has_unique_resolved_vin("1HGBH41JXMN109186") is False


# ─── VT-7: Integration — mocked IMAP + mocked worksheet ──────────────────────

class TestVT7_IntegrationMocked:
    """VT-7: End-to-end with mocked IMAP and mocked gspread worksheet."""

    def _build_monitor(self, tmp_path: Path):
        from vendor_tracking.monitor import VendorTrackingMonitor
        config = {
            "imap_server": "imap.test.local",
            "email_account": "test@test.com",
            "email_password": "secret",
            "vendor_tracking_spreadsheet_id": "fake_sheet_id",
            "vendor_tracking_sheet_name": "GlassClaims",
            "vendor_tracking_senders": ["autoglassnow.com"],
            "vendor_tracking_lookback_days": 7,
            "vendor_tracking_idempotency_store": str(tmp_path / "idem.json"),
            "vendor_tracking_decision_log": str(tmp_path / "decisions.jsonl"),
        }
        monitor = VendorTrackingMonitor(config)
        # Inject idempotency store pointing to tmp
        from vendor_tracking.idempotency_store import IdempotencyStore
        monitor._idempotency_path = tmp_path / "idem.json"
        monitor._store = IdempotencyStore(tmp_path / "idem.json")
        return monitor

    def test_approval_needed_email_surfaces_in_approval_list(self, tmp_path):
        monitor = self._build_monitor(tmp_path)

        approval_msg = _make_message(
            sender="agn@autoglassnow.com",
            subject="Please Advise — Prior Approval Required",
            body=APPROVAL_HTML,
            message_id="<approval-001@autoglassnow.com>",
        )

        headers = [
            "Inventory Date", "MVA", "FPO#", "VIN", "Make",
            "Repair Status", "Approval Needed", "Cost", "Repair Status Notes", "Vendor Job Number",
        ]
        data_rows = [
            ["05/06/2026", "12345678", "FPO001", "1HGBH41JXMN109186", "Honda",
             "", "", "", "", ""],
        ]
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [headers] + data_rows

        updater = VendorSheetUpdater.__new__(VendorSheetUpdater)
        updater._spreadsheet_id = "fake"
        updater._sheet_name = "GlassClaims"
        updater._service_account_json = "fake.json"
        updater._ws = mock_ws
        updater._headers = headers[:]
        updater._all_values = [headers] + data_rows
        monitor._updater = updater

        from vendor_tracking.monitor import RunSummary
        summary = RunSummary()

        html = APPROVAL_HTML
        monitor._handle_approval_needed(html, "<approval-001@autoglassnow.com>", "Please Advise", summary)

        assert len(summary.approval_needed) >= 1
        assert any("1HGBH41JXMN109186" in vin for vin in summary.approval_needed)

    def test_unknown_sender_not_processed(self, tmp_path):
        monitor = self._build_monitor(tmp_path)
        msg = _make_message(
            sender="spam@unknown.com",
            subject="Buy something",
            body="<p>Hello</p>",
            message_id="<spam-001@unknown.com>",
        )
        assert classify_email(msg) == EmailType.UNKNOWN

    def test_approval_needed_skips_when_row_already_resolved(self, tmp_path):
        monitor = self._build_monitor(tmp_path)

        headers = [
            "Inventory Date", "MVA", "FPO#", "VIN", "Make",
            "Repair Status", "Approval Needed", "Cost", "Repair Status Notes", "Vendor Job Number",
        ]
        data_rows = [
            ["05/06/2026", "12345678", "FPO001", "1HGBH41JXMN109186", "Honda",
             STATUS_COMPLETED, "No", "", "", ""],
        ]
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [headers] + data_rows

        updater = VendorSheetUpdater.__new__(VendorSheetUpdater)
        updater._spreadsheet_id = "fake"
        updater._sheet_name = "GlassClaims"
        updater._service_account_json = "fake.json"
        updater._ws = mock_ws
        updater._headers = headers[:]
        updater._all_values = [headers] + data_rows
        monitor._updater = updater

        from vendor_tracking.monitor import RunSummary
        summary = RunSummary()

        monitor._handle_approval_needed(APPROVAL_HTML, "<approval-002@autoglassnow.com>", "Please Advise", summary)

        # No field writes should occur on resolved rows.
        assert mock_ws.update_cell.call_count == 0
        # We should not surface this as active approval-needed once resolved.
        assert summary.approval_needed == []


# ─── VT-8: Idempotency store ─────────────────────────────────────────────────

class TestVT8_Idempotency:
    """VT-8: IdempotencyStore — no duplicate writes on rerun."""

    def test_new_id_not_processed(self, tmp_path):
        store = IdempotencyStore(tmp_path / "idem.json")
        assert not store.is_processed("<msg-001@test.com>")

    def test_mark_then_check(self, tmp_path):
        store = IdempotencyStore(tmp_path / "idem.json")
        store.mark_processed("<msg-001@test.com>")
        assert store.is_processed("<msg-001@test.com>")

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "idem.json"
        store1 = IdempotencyStore(path)
        store1.mark_processed("<msg-001@test.com>")

        store2 = IdempotencyStore(path)
        assert store2.is_processed("<msg-001@test.com>")

    def test_multiple_ids_independent(self, tmp_path):
        store = IdempotencyStore(tmp_path / "idem.json")
        store.mark_processed("<msg-001@test.com>")
        assert not store.is_processed("<msg-002@test.com>")

    def test_duplicate_mark_does_not_grow_set(self, tmp_path):
        store = IdempotencyStore(tmp_path / "idem.json")
        store.mark_processed("<msg-001@test.com>")
        store.mark_processed("<msg-001@test.com>")
        assert len(store) == 1


class TestCompletionReceiptQueue:

    def _build_monitor(self, tmp_path, *, replay=False):
        from vendor_tracking.monitor import VendorTrackingMonitor

        return VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": str(tmp_path / "idem.json"),
                "vendor_tracking_close_csv": str(tmp_path / "close_workitem.csv"),
                "vendor_tracking_decision_log": str(tmp_path / "decisions.jsonl"),
            },
            replay_completion_receipts=replay,
        )

    def test_unique_vin_queues_candidate_and_updates_sheet(self, tmp_path):
        from vendor_tracking.monitor import RunSummary

        monitor = self._build_monitor(tmp_path)
        updater = MagicMock()
        updater.find_unique_row_by_vin.return_value = type(
            "Match", (), {"is_ok": True, "row_index": 7, "status": "ok", "note": ""}
        )()
        updater.get_row_fields.return_value = {"MVA": "12345678", "Damage Type": "Replacement"}
        monitor._updater = updater
        summary = RunSummary()

        monitor._handle_completion_receipt(
            COMPLETION_HTML,
            "<completion-001@autoglassnow.com>",
            "Receipt for Job #4946751",
            summary,
        )

        assert (tmp_path / "close_workitem.csv").read_text(encoding="utf-8").splitlines() == [
            "mva,Type",
            "012345678,Glass",
        ]
        updater.update_vendor_fields.assert_called_once_with(
            7,
            {
                "Repair Status": STATUS_COMPLETED,
                "Vendor Job Number": "4946751",
                "Cost": "$306.00",
            },
        )
        assert summary.close_candidates == ["012345678"]

    def test_duplicate_candidate_is_not_appended_twice(self, tmp_path):
        from vendor_tracking.monitor import RunSummary

        monitor = self._build_monitor(tmp_path)
        updater = MagicMock()
        updater.find_unique_row_by_vin.return_value = type(
            "Match", (), {"is_ok": True, "row_index": 7, "status": "ok", "note": ""}
        )()
        updater.get_row_fields.return_value = {"MVA": "12345678", "Damage Type": "Repair"}
        monitor._updater = updater

        monitor._handle_completion_receipt(COMPLETION_HTML, "<one>", "Receipt for Job #4946751", RunSummary())
        monitor._handle_completion_receipt(COMPLETION_HTML, "<two>", "Receipt for Job #4946751", RunSummary())

        assert (tmp_path / "close_workitem.csv").read_text(encoding="utf-8").count("012345678,Glass") == 1

    def test_ambiguous_vin_never_queues_or_updates(self, tmp_path):
        from vendor_tracking.monitor import RunSummary

        monitor = self._build_monitor(tmp_path)
        updater = MagicMock()
        updater.find_unique_row_by_vin.return_value = type(
            "Match",
            (),
            {"is_ok": False, "row_index": None, "status": "ambiguous", "note": "VIN matched 2 rows"},
        )()
        monitor._updater = updater
        summary = RunSummary()

        monitor._handle_completion_receipt(
            COMPLETION_HTML,
            "<completion-ambiguous>",
            "Receipt for Job #4946751",
            summary,
        )

        assert not (tmp_path / "close_workitem.csv").exists()
        updater.update_vendor_fields.assert_not_called()
        assert summary.close_candidates == []
        assert "ambiguous" in summary.needs_review[0]

    def test_completion_replay_bypasses_processed_gate_only_for_receipt(self, tmp_path):
        from vendor_tracking.monitor import RunSummary

        monitor = self._build_monitor(tmp_path, replay=True)
        message_id = "<completion-replay@autoglassnow.com>"
        monitor._store._ids.add(message_id)
        monitor._handle_completion_receipt = MagicMock()
        msg = _make_message(
            subject="Receipt for Job #4946751",
            body=COMPLETION_HTML,
            message_id=message_id,
        )
        summary = RunSummary()

        monitor._process_message(msg, summary)

        monitor._handle_completion_receipt.assert_called_once()
        assert summary.skipped_idempotent == 0


# ─── VT-9: Approval Needed blocker in run summary ─────────────────────────────

class TestVT9_ApprovalNeededSummary:
    """VT-9: Approval Needed blocker surfaced prominently in run summary output."""

    def test_approval_needed_vins_in_summary(self, capsys):
        from vendor_tracking.monitor import RunSummary, _print_summary

        summary = RunSummary()
        summary.total_fetched = 5
        summary.processed = 2
        summary.approval_needed = ["1HGBH41JXMN109186", "2HGBH41JXMN109187"]

        _print_summary(summary)
        captured = capsys.readouterr()

        assert "APPROVAL NEEDED" in captured.out
        assert "1HGBH41JXMN109186" in captured.out
        assert "2HGBH41JXMN109187" in captured.out

    def test_no_approval_needed_section_when_empty(self, capsys):
        from vendor_tracking.monitor import RunSummary, _print_summary

        summary = RunSummary()
        summary.total_fetched = 3
        summary.processed = 3

        _print_summary(summary)
        captured = capsys.readouterr()

        assert "APPROVAL NEEDED" not in captured.out


# ─── VT-10: Replay / idempotency controls ───────────────────────────────────

class TestVT10_ReplayControls:
    """VT-10: since-date and idempotency replay controls."""

    def test_since_date_mdy_format_is_converted_to_imap(self):
        from vendor_tracking.monitor import VendorTrackingMonitor

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            since_date="05/04/2026",
            ignore_idempotency=False,
        )
        assert monitor._resolve_imap_since_date() == "04-May-2026"

    def test_blank_vendor_sheet_override_uses_main_sheet_config(self):
        from vendor_tracking.monitor import VendorTrackingMonitor

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_spreadsheet_id": "",
                "spreadsheet_id": "main-sheet-id",
                "vendor_tracking_sheet_name": "",
                "sheet_name": "GlassClaims",
            }
        )

        assert monitor._spreadsheet_id == "main-sheet-id"
        assert monitor._sheet_name == "GlassClaims"

    def test_since_date_iso_format_is_converted_to_imap(self):
        from vendor_tracking.monitor import VendorTrackingMonitor

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            since_date="2026-05-04",
            ignore_idempotency=False,
        )
        assert monitor._resolve_imap_since_date() == "04-May-2026"

    def test_invalid_since_date_raises_runtime_error(self):
        from vendor_tracking.monitor import VendorTrackingMonitor

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            since_date="not-a-date",
            ignore_idempotency=False,
        )
        with pytest.raises(RuntimeError):
            monitor._resolve_imap_since_date()

    def test_ignore_idempotency_does_not_skip_processed_message(self):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        msg = _make_message(
            sender="agn@autoglassnow.com",
            subject="Please Advise — Prior Approval Required",
            body=APPROVAL_HTML,
            message_id="<replay-001@autoglassnow.com>",
        )

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            ignore_idempotency=True,
        )
        # Pretend the message is already processed.
        monitor._store._ids.add("<replay-001@autoglassnow.com>")

        # Replace handlers so we can verify processing path executes.
        called = {"approval": 0}

        def _fake_handle_approval(html, message_id, subject, summary):
            called["approval"] += 1

        monitor._handle_approval_needed = _fake_handle_approval  # type: ignore[assignment]

        summary = RunSummary()
        monitor._process_message(msg, summary)

        assert called["approval"] == 1
        assert summary.skipped_idempotent == 0
        assert summary.processed == 1

    def test_dry_run_does_not_skip_processed_message(self):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        msg = _make_message(
            sender="agn@autoglassnow.com",
            subject="Please Advise — Prior Approval Required",
            body=APPROVAL_HTML,
            message_id="<replay-002@autoglassnow.com>",
        )

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            dry_run=True,
        )
        monitor._store._ids.add("<replay-002@autoglassnow.com>")

        called = {"approval": 0}

        def _fake_handle_approval(html, message_id, subject, summary):
            called["approval"] += 1

        monitor._handle_approval_needed = _fake_handle_approval  # type: ignore[assignment]

        summary = RunSummary()
        monitor._process_message(msg, summary)

        assert called["approval"] == 1
        assert summary.skipped_idempotent == 0
        assert summary.processed == 1

    def test_dry_run_does_not_mark_idempotency_store(self):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        msg = _make_message(
            sender="agn@autoglassnow.com",
            subject="Please Advise — Prior Approval Required",
            body=APPROVAL_HTML,
            message_id="<replay-003@autoglassnow.com>",
        )

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            dry_run=True,
        )

        def _fake_handle_approval(html, message_id, subject, summary):
            return

        monitor._handle_approval_needed = _fake_handle_approval  # type: ignore[assignment]

        summary = RunSummary()
        monitor._process_message(msg, summary)

        assert monitor._store.is_processed("<replay-003@autoglassnow.com>") is False

    def test_dry_run_would_update_row_without_write(self, tmp_path):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
                "vendor_tracking_decision_log": str(tmp_path / "decisions.jsonl"),
            },
            dry_run=True,
        )

        updater = MagicMock()
        updater.has_unique_resolved_vin.return_value = False
        updater.find_row.return_value = type("M", (), {"is_ok": True, "row_index": 3, "status": "ok", "note": ""})()
        updater.is_row_resolved.return_value = False
        monitor._updater = updater

        summary = RunSummary()
        monitor._handle_approval_needed(APPROVAL_HTML, "<replay-004@autoglassnow.com>", "Please Advise", summary)

        updater.update_vendor_fields.assert_not_called()
        assert summary.approval_needed == ["1HGBH41JXMN109186"]


class TestVT11_DecisionLogging:
    """VT-11: JSONL decision audit trail is written with expected outcomes."""

    def test_writes_skip_idempotent_decision(self, tmp_path):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        decision_log = tmp_path / "decisions.jsonl"
        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            decision_log_path=str(decision_log),
        )
        msg = _make_message(message_id="<dlog-001@test.com>", subject="Any", body="<p>x</p>")
        monitor._store._ids.add("<dlog-001@test.com>")

        summary = RunSummary()
        monitor._process_message(msg, summary)

        lines = decision_log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["decision"] == "skip_idempotent"

    def test_writes_resolved_skip_decision(self, tmp_path):
        from vendor_tracking.monitor import VendorTrackingMonitor, RunSummary

        decision_log = tmp_path / "decisions.jsonl"
        monitor = VendorTrackingMonitor(
            {
                "vendor_tracking_lookback_days": 30,
                "vendor_tracking_idempotency_store": "data/test_idem.json",
            },
            decision_log_path=str(decision_log),
            ignore_idempotency=True,
        )

        updater = MagicMock()
        updater.has_unique_resolved_vin.return_value = True
        monitor._updater = updater

        summary = RunSummary()
        monitor._handle_approval_needed(APPROVAL_HTML, "<dlog-002@test.com>", "Please Advise", summary)

        lines = decision_log.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[-1])
        assert payload["decision"] == "skip_row_resolved"
        assert payload["email_type"] == "APPROVAL_NEEDED"
