"""
Unit Tests for GlassOrchestrator — Phase-level logic without external dependencies.

UT-1: Suffix Regex Accuracy
UT-2: HTML Extraction
UT-3: Idempotency Check
UT-4: Sanitization
UT-7: Location Extraction from Type Column
"""

import re
import json
from datetime import datetime

import pandas as pd
import pytest

import GlassOrchestrator
from GlassOrchestrator import (
    MVA_PATTERN,
    _load_local_config_overrides,
    _load_runtime_config,
    _extract_body,
    _extract_internaldate_from_fetch_response,
    _extract_location_from_type,
    _parse_email_datetime,
    _parse_html_descriptions,
    _validate_email_credentials,
    notify_order_items,
    is_duplicate,
    parse_descriptions_to_manifest,
)


# ─── UT-1: Suffix Regex Accuracy ─────────────────────────────────────────────


class TestUT1_SuffixRegex:
    """Verify the parser correctly extracts the 8-digit MVA and maps
    Action / Area / Claim# for all four flag combinations.
    Scan format: <MVA:8digits><AREA_ID:uppercase>[r][c]"""

    def test_plain_mva(self):
        """59340120WS → group 1=MVA, 2=AREA_ID, 3=empty, 4=empty"""
        m = MVA_PATTERN.match("59340120WS")
        assert m is not None
        assert m.group(1) == "59340120"
        assert m.group(2) == "WS"
        assert m.group(3) == ""
        assert m.group(4) == ""

    def test_repair_suffix(self):
        """59340120WSr → repair flag captured in group 3"""
        m = MVA_PATTERN.match("59340120WSr")
        assert m is not None
        assert m.group(1) == "59340120"
        assert m.group(2) == "WS"
        assert m.group(3) == "r"
        assert m.group(4) == ""

    def test_claim_suffix(self):
        """59340120WSc → claim flag captured in group 4"""
        m = MVA_PATTERN.match("59340120WSc")
        assert m is not None
        assert m.group(1) == "59340120"
        assert m.group(2) == "WS"
        assert m.group(3) == ""
        assert m.group(4) == "c"

    def test_lowercase_scan_matches_case_insensitively(self):
        """59193750wsc → parser accepts scanner-lowercase area/suffix values."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59193750wsc")], datetime(2026, 3, 5)
        )
        assert manifest["59193750"]["Action"] == "Replacement"
        assert manifest["59193750"]["Area"] == "Windshield"
        assert manifest["59193750"]["Claim#"] == "Listed"
        assert mva_list == ["59193750"]

    def test_both_suffixes(self):
        """59340120WSrc → both flags captured"""
        m = MVA_PATTERN.match("59340120WSrc")
        assert m is not None
        assert m.group(1) == "59340120"
        assert m.group(2) == "WS"
        assert m.group(3) == "r"
        assert m.group(4) == "c"

    def test_no_area_code_does_not_match(self):
        """Bare MVA with no area code must not match (AREA_ID required)."""
        assert MVA_PATTERN.match("59340120") is None

    def test_phase2_mapping_plain(self):
        """End-to-end: 59340120WS → Replacement, WS, Missing"""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59340120WS")], datetime(2026, 3, 5)
        )
        assert "59340120" in manifest
        assert manifest["59340120"]["Action"] == "Replacement"
        assert manifest["59340120"]["Area"] == "Windshield"
        assert manifest["59340120"]["Claim#"] == "Missing"
        assert manifest["59340120"]["Location"] == "APO"
        assert mva_list == ["59340120"]

    def test_phase2_mapping_repair(self):
        """59340120WSr → Repair, Windshield, Missing"""
        manifest, _ = parse_descriptions_to_manifest([("0305APO", "59340120WSr")], datetime(2026, 3, 5))
        assert manifest["59340120"]["Action"] == "Repair"
        assert manifest["59340120"]["Area"] == "Windshield"
        assert manifest["59340120"]["Claim#"] == "Missing"

    def test_phase2_mapping_claim(self):
        """59340120WSc → Replacement, Windshield, Listed"""
        manifest, _ = parse_descriptions_to_manifest([("0305APO", "59340120WSc")], datetime(2026, 3, 5))
        assert manifest["59340120"]["Action"] == "Replacement"
        assert manifest["59340120"]["Area"] == "Windshield"
        assert manifest["59340120"]["Claim#"] == "Listed"

class TestEmailCredentialValidation:
    """Validate Gmail credential handling before IMAP login."""

    def test_validate_email_credentials_raises_when_account_missing(self):
        with pytest.raises(ValueError, match="GLASS_EMAIL_ACCOUNT"):
            _validate_email_credentials("", "secret")

    def test_validate_email_credentials_raises_when_password_missing(self):
        with pytest.raises(ValueError, match="GLASS_EMAIL_PASSWORD"):
            _validate_email_credentials("user@example.com", "")

    def test_validate_email_credentials_accepts_nonempty_values(self):
        _validate_email_credentials("user@example.com", "secret")

    def test_production_failure_missing_account(self):
        """Regression: detect production failure when account is empty."""
        with pytest.raises(ValueError, match="Missing Gmail credentials"):
            _validate_email_credentials("", "secret")

    def test_production_failure_missing_password(self):
        """Regression: detect production failure when password is empty."""
        with pytest.raises(ValueError, match="Missing Gmail credentials"):
            _validate_email_credentials("user@example.com", "")

    def test_production_failure_missing_credentials(self):
        """Regression: detect production failure when both credentials are empty."""
        with pytest.raises(ValueError, match="Missing Gmail credentials"):
            _validate_email_credentials("", "")

    def test_production_env_account_must_be_set(self):
        """Fail if actual loaded EMAIL_ACCOUNT is empty (production requirement)."""
        assert GlassOrchestrator.EMAIL_ACCOUNT, "EMAIL_ACCOUNT environment variable or config value must be set"

    def test_production_env_password_must_be_set(self):
        """Fail if actual loaded EMAIL_PASSWORD is empty (production requirement)."""
        assert GlassOrchestrator.EMAIL_PASSWORD, "EMAIL_PASSWORD environment variable or config value must be set"

    def test_gmail_credentials_valid(self):
        """Validate that Gmail IMAP credentials are correct by attempting login."""
        import imaplib
        
        try:
            mail = imaplib.IMAP4_SSL(GlassOrchestrator.IMAP_SERVER)
            mail.login(GlassOrchestrator.EMAIL_ACCOUNT, GlassOrchestrator.EMAIL_PASSWORD)
            mail.logout()
        except imaplib.IMAP4.error as e:
            pytest.fail(f"Gmail IMAP login failed: {e}")

    def test_phase2_mapping_both(self):
        """59340120WSrc → Repair, Windshield, Listed"""
        manifest, _ = parse_descriptions_to_manifest([("0305APO", "59340120WSrc")], datetime(2026, 3, 5))
        assert manifest["59340120"]["Action"] == "Repair"
        assert manifest["59340120"]["Area"] == "Windshield"
        assert manifest["59340120"]["Claim#"] == "Listed"

    def test_phase2_all_four_variations(self):
        """Process all four flag combinations in one batch."""
        descriptions = [
            ("0305APO", "59340120WS"),    # Replacement, Windshield, Missing
            ("0305APO", "59340121WSr"),   # Repair, Windshield, Missing (WS only)
            ("0305APO", "59340122BWc"),   # Replacement, Back Window, Listed
            ("0305APO", "59340123WSrc"),  # Repair, Windshield, Listed
        ]
        manifest, mva_list = parse_descriptions_to_manifest(descriptions, datetime(2026, 3, 5))
        assert len(manifest) == 4
        assert len(mva_list) == 4
        assert manifest["59340120"]["Action"] == "Replacement"
        assert manifest["59340120"]["Area"] == "Windshield"
        assert manifest["59340120"]["Claim#"] == "Missing"
        assert manifest["59340121"]["Action"] == "Repair"
        assert manifest["59340121"]["Area"] == "Windshield"
        assert manifest["59340121"]["Claim#"] == "Missing"
        assert manifest["59340122"]["Action"] == "Replacement"
        assert manifest["59340122"]["Area"] == "Back Window"
        assert manifest["59340122"]["Claim#"] == "Listed"
        assert manifest["59340123"]["Action"] == "Repair"
        assert manifest["59340123"]["Area"] == "Windshield"
        assert manifest["59340123"]["Claim#"] == "Listed"


# ─── UT-1b: Scan Error Codes ─────────────────────────────────────────────────


class TestUT1b_ScanErrorCodes:
    """Error scans are logged and excluded from processing.
    The manifest must NOT contain any entry for an errored scan."""

    def test_ambiguous_location(self):
        """Unknown area code → AMBIGUOUS_LOCATION logged, scan excluded."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59340120ZZ")], datetime(2026, 3, 5)
        )
        assert manifest == {}
        assert mva_list == []

    def test_invalid_repair_on_non_windshield(self):
        """Repair flag on non-repair-eligible area → INVALID_REPAIR logged, scan excluded."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59340120BWr")], datetime(2026, 3, 5)
        )
        assert manifest == {}
        assert mva_list == []

    def test_malformed_scan(self):
        """Unparseable scan string → MALFORMED_SCAN logged, scan excluded."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "garbage")], datetime(2026, 3, 5)
        )
        assert manifest == {}
        assert mva_list == []

    def test_spec_example_wsrc(self):
        """59193750WSrc → Repair, Windshield, Listed (spec example 1)."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59193750WSrc")], datetime(2026, 3, 5)
        )
        assert manifest["59193750"]["Action"] == "Repair"
        assert manifest["59193750"]["Area"] == "Windshield"
        assert manifest["59193750"]["Claim#"] == "Listed"
        assert mva_list == ["59193750"]

    def test_spec_example_fldc(self):
        """59536396FLDc → Replacement, Front Left Door, Listed (spec example 2)."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "59536396FLDc")], datetime(2026, 3, 5)
        )
        assert manifest["59536396"]["Action"] == "Replacement"
        assert manifest["59536396"]["Area"] == "Front Left Door"
        assert manifest["59536396"]["Claim#"] == "Listed"
        assert mva_list == ["59536396"]

    def test_spec_example_bw(self):
        """61066902BW → Replacement, Back Window, Missing (spec example 3)."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0305APO", "61066902BW")], datetime(2026, 3, 5)
        )
        assert manifest["61066902"]["Action"] == "Replacement"
        assert manifest["61066902"]["Area"] == "Back Window"
        assert manifest["61066902"]["Claim#"] == "Missing"
        assert mva_list == ["61066902"]

    def test_frw_area_label(self):
        """59400084FRWc → Replacement, Front Right Window, Listed."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0427APO", "59400084FRWc")], datetime(2026, 4, 27)
        )
        assert manifest["59400084"]["Action"] == "Replacement"
        assert manifest["59400084"]["Area"] == "Front Right Window"
        assert manifest["59400084"]["Claim#"] == "Listed"
        assert mva_list == ["59400084"]


# ─── UT-2: HTML Extraction ────────────────────────────────────────────────────


class TestUT2_HTMLExtraction:
    """Verify that Description column values are correctly extracted
    from a mock HTML table representing an Orca Scan email body."""

    MOCK_HTML = """
    <html><body>
    <table>
      <tr><th>Type</th><th>Date</th><th>MVA</th><th>Description</th><th>Other</th></tr>
      <tr><td>batch1</td><td>2026-03-05</td><td>001</td><td>59340120</td><td>X</td></tr>
      <tr><td>batch1</td><td>2026-03-05</td><td>002</td><td>59340121r</td><td>Y</td></tr>
      <tr><td>batch1</td><td>2026-03-05</td><td>003</td><td>59340122rc</td><td>Z</td></tr>
    </table>
    </body></html>
    """

    # Orca Scan packs multiple MVAs into a single Description cell with newlines
    ORCA_HTML = """
    <html><body>
    <table id="rowData" cellspacing="0" cellpadding="4" border="1">
      <thead>
        <tr>
          <th>Type</th><th>Name</th><th>Description</th>
          <th>Quantity</th><th>Storage Area</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>0205</td><td></td><td>59340120c
58157002
58135663cr
57193500r</td>
          <td>1</td><td></td>
        </tr>
      </tbody>
    </table>
    </body></html>
    """

    def test_extracts_description_column(self):
        result = _parse_html_descriptions(self.MOCK_HTML)
        assert result == [("batch1", "59340120"), ("batch1", "59340121r"), ("batch1", "59340122rc")]

    def test_orca_multiline_cell_splits_into_individual_mvas(self):
        result = _parse_html_descriptions(self.ORCA_HTML)
        assert result == [("0205", "59340120c"), ("0205", "58157002"), ("0205", "58135663cr"), ("0205", "57193500r")]

    def test_returns_empty_on_no_table(self):
        result = _parse_html_descriptions("<html><body><p>No table</p></body></html>")
        assert result == []

    def test_returns_empty_on_no_description_header(self):
        html = """
        <table>
          <tr><th>Date</th><th>MVA</th><th>Notes</th></tr>
          <tr><td>2026-03-05</td><td>001</td><td>foo</td></tr>
        </table>
        """
        result = _parse_html_descriptions(html)
        assert result == []

    def test_skips_empty_description_cells(self):
        html = """
        <table>
          <tr><th>Type</th><th>Description</th></tr>
          <tr><td>batch1</td><td>59340120</td></tr>
          <tr><td>batch1</td><td></td></tr>
          <tr><td>batch1</td><td>59340121r</td></tr>
        </table>
        """
        result = _parse_html_descriptions(html)
        assert result == [("batch1", "59340120"), ("batch1", "59340121r")]

    def test_extract_body_prefers_html_with_table(self):
        """_extract_body should return HTML when it contains a <table>."""
        import email as email_mod
        msg = email_mod.mime.multipart.MIMEMultipart("mixed")
        from email.mime.text import MIMEText
        msg.attach(MIMEText("<table><tr><th>Description</th></tr></table>", "html"))
        msg.attach(MIMEText("plain text fallback", "plain"))
        body = _extract_body(msg)
        assert "<table>" in body

    def test_name_column_fallback_when_description_empty(self):
        """When Description cells are empty, MVAs are read from the Name column."""
        html = """
        <table id="rowData">
          <tr><th>Type</th><th>Name</th><th>Description</th></tr>
          <tr><td>0427APO</td><td>56642073WSrc 60425330WSrc</td><td></td></tr>
        </table>
        """
        result = _parse_html_descriptions(html)
        assert ("0427APO", "56642073WSrc") in result
        assert ("0427APO", "60425330WSrc") in result

    def test_description_preferred_over_name(self):
        """When Description has content it is preferred over Name."""
        html = """
        <table>
          <tr><th>Type</th><th>Name</th><th>Description</th></tr>
          <tr><td>0305APO</td><td>NameMVA</td><td>59340120WSc</td></tr>
        </table>
        """
        result = _parse_html_descriptions(html)
        assert result == [("0305APO", "59340120WSc")]

        def test_type_column_recovery_when_name_and_description_empty(self):
                """Recover scans when malformed export places all data in Type column."""
                html = """
                <table>
                    <tr>
                        <th>Type</th><th>Name</th><th>Description</th><th>Quantity</th><th>Date</th>
                    </tr>
                    <tr>
                        <td>60565595SR 57780052WSr 61368742WS 60148373WS 60382092WS 58861235WS 61744550RLD 55863205WS 59754505WS 54036953r</td>
                        <td></td><td></td><td>1</td><td>07/27/2026</td>
                    </tr>
                </table>
                """
                result = _parse_html_descriptions(html)
                assert result == [
                        ("", "60565595SR"),
                        ("", "57780052WSr"),
                        ("", "61368742WS"),
                        ("", "60148373WS"),
                        ("", "60382092WS"),
                        ("", "58861235WS"),
                        ("", "61744550RLD"),
                        ("", "55863205WS"),
                        ("", "59754505WS"),
                        ("", "54036953r"),
                ]


# ─── UT-3: Idempotency Check ─────────────────────────────────────────────────


class TestUT3_Idempotency:
    """Verify deprecated duplicate checks always return False with no-dedup policy."""

    MOCK_EXISTING = {
        "59340120|2026-03-05",
        "59340121|2026-03-05",
        "59340122|2026-03-04",
    }

    def test_duplicate_returns_false(self):
        assert is_duplicate("59340120", "03/05/2026", self.MOCK_EXISTING) is False

    def test_new_mva_returns_false(self):
        assert is_duplicate("99999999", "03/05/2026", self.MOCK_EXISTING) is False

    def test_same_mva_different_date_returns_false(self):
        assert is_duplicate("59340120", "03/06/2026", self.MOCK_EXISTING) is False

    def test_empty_existing_returns_false(self):
        assert is_duplicate("59340120", "03/05/2026", set()) is False

    def test_non_padded_date_returns_false(self):
        assert is_duplicate("59340120", "3/5/2026", self.MOCK_EXISTING) is False


# ─── UT-4: Sanitization ──────────────────────────────────────────────────────


class TestUT4_Sanitization:
    """Ensure malformed entries do not reach the worker MVA list.
    Malformed scans are logged and excluded from both the manifest and mva_list."""

    def test_too_short(self):
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "12345")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_no_digits(self):
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "abcdefgh")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_too_long(self):
        """9 numeric digits with no valid area code — MALFORMED_SCAN."""
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "123456789")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_mixed_valid_and_invalid(self):
        """Valid scans with area codes reach mva_list; invalid scans are silently excluded."""
        descriptions = [
            ("0305APO", "59340120WS"),   # valid
            ("0305APO", "12345"),          # too short → excluded
            ("0305APO", "abcdefgh"),       # no digits → excluded
            ("0305APO", "59340121WSr"),   # valid
        ]
        manifest, mva_list = parse_descriptions_to_manifest(descriptions, datetime(2026, 3, 5))
        assert len(manifest) == 2   # only valid entries
        assert set(mva_list) == {"59340120", "59340121"}

    def test_special_characters(self):
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "5934012!")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_whitespace_only(self):
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "   ")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_invalid_suffix(self):
        """Lowercase 'x' is not a valid area code — MALFORMED_SCAN, excluded."""
        manifest, mva_list = parse_descriptions_to_manifest([("0305APO", "59340120x")], datetime(2026, 3, 5))
        assert manifest == {}
        assert mva_list == []

    def test_lowercase_current_area_alias_is_accepted(self):
        """Current scanner codes like 51085661lfd parse to the matching door area."""
        manifest, mva_list = parse_descriptions_to_manifest(
            [("0505apo", "51085661lfd")], datetime(2026, 5, 5)
        )
        assert manifest["51085661"]["Action"] == "Replacement"
        assert manifest["51085661"]["Area"] == "Front Left Door"
        assert manifest["51085661"]["Location"] == "APO"
        assert manifest["51085661"]["Claim#"] == "Missing"
        assert mva_list == ["51085661"]


# ─── UT-5: Local Config Overrides ───────────────────────────────────────────


class TestUT5_LocalConfigOverrides:
    """Validate local override loading and merge precedence behavior."""

    def test_missing_local_config_returns_empty_dict(self, tmp_path):
        local_path = tmp_path / "orchestrator_config.local.json"
        loaded = _load_local_config_overrides(local_path)
        assert loaded == {}

    def test_invalid_json_local_config_returns_empty_dict(self, tmp_path, caplog):
        local_path = tmp_path / "orchestrator_config.local.json"
        local_path.write_text("{invalid", encoding="utf-8")

        loaded = _load_local_config_overrides(local_path)

        assert loaded == {}
        assert "Local config override load failed" in caplog.text

    def test_non_dict_local_config_returns_empty_dict(self, tmp_path):
        local_path = tmp_path / "orchestrator_config.local.json"
        local_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

        loaded = _load_local_config_overrides(local_path)
        assert loaded == {}

    def test_valid_local_config_returns_dict(self, tmp_path):
        local_path = tmp_path / "orchestrator_config.local.json"
        local_path.write_text(
            json.dumps({"sheet_name": "LocalGlassClaims", "location": "BOS"}),
            encoding="utf-8",
        )

        loaded = _load_local_config_overrides(local_path)
        assert loaded == {"sheet_name": "LocalGlassClaims", "location": "BOS"}

    def test_local_overrides_take_precedence_over_runtime_config(self, tmp_path):
        runtime_path = tmp_path / "orchestrator_config.json"
        local_path = tmp_path / "orchestrator_config.local.json"

        runtime_path.write_text(
            json.dumps(
                {
                    "sheet_name": "BaseSheet",
                    "location": "APO",
                    "cycle_gap_grace_days": 7,
                }
            ),
            encoding="utf-8",
        )
        local_path.write_text(
            json.dumps({"sheet_name": "LocalSheet", "location": "BOS"}),
            encoding="utf-8",
        )

        merged = _load_runtime_config(runtime_path)
        merged.update(_load_local_config_overrides(local_path))

        assert merged["sheet_name"] == "LocalSheet"
        assert merged["location"] == "BOS"
        # Value present only in runtime config should be preserved.
        assert merged["cycle_gap_grace_days"] == 7

    def test_legacy_local_then_shared_local_override_order(self, tmp_path):
        runtime_path = tmp_path / "orchestrator_config.json"
        shared_local_path = tmp_path / "config.local.json"
        legacy_local_path = tmp_path / "orchestrator_config.local.json"

        runtime_path.write_text(
            json.dumps({"location": "APO", "email_account": "base@company.com"}),
            encoding="utf-8",
        )
        shared_local_path.write_text(
            json.dumps({"email_account": "shared@company.com", "sheet_name": "SharedSheet"}),
            encoding="utf-8",
        )
        legacy_local_path.write_text(
            json.dumps({"email_account": "legacy@company.com"}),
            encoding="utf-8",
        )

        merged = _load_runtime_config(runtime_path)
        merged.update(_load_local_config_overrides(legacy_local_path))
        merged.update(_load_local_config_overrides(shared_local_path))

        # Shared local file can provide orchestrator values.
        assert merged["sheet_name"] == "SharedSheet"
        # Shared local file wins when both define the same key.
        assert merged["email_account"] == "shared@company.com"


# ─── UT-5b: Email Datetime Resolution ──────────────────────────────────────


class TestUT5b_EmailDatetimeResolution:
    """Validate deterministic timestamp resolution for inbound email parsing."""

    def test_parse_email_datetime_uses_internaldate_fallback_when_header_missing(self):
        fallback = datetime(2026, 5, 2, 13, 45, 0)
        assert _parse_email_datetime("", fallback_sent_at=fallback) == fallback

    def test_parse_email_datetime_uses_internaldate_fallback_when_header_invalid(self):
        fallback = datetime(2026, 5, 2, 13, 45, 0)
        assert _parse_email_datetime("not-a-date", fallback_sent_at=fallback) == fallback

    def test_parse_email_datetime_raises_when_no_valid_source(self):
        with pytest.raises(ValueError, match="timestamp unavailable"):
            _parse_email_datetime("not-a-date")

    def test_extract_internaldate_from_fetch_response(self):
        msg_data = [
            (
                b'123 (RFC822 {42} INTERNALDATE "02-May-2026 15:30:00 +0000")',
                b"Subject: Test\r\n\r\nBody",
            )
        ]
        parsed = _extract_internaldate_from_fetch_response(msg_data)
        assert parsed is not None
        assert parsed.year == 2026
        assert parsed.month == 5
        assert parsed.day == 2


# ─── UT-6: Notification Payload ─────────────────────────────────────────────


class TestUT6_NotificationPayload:
    """Ensure notification email includes all persisted rows."""

    def test_notify_includes_replacement_and_repair_rows(self, monkeypatch):
        df = pd.DataFrame(
            [
                {
                    "Inventory Date": "03/09/2026",
                    "Original Date": "03/09/2026",
                    "MVA": "59654641",
                    "VIN": "1HGCY1F44SA083453",
                    "Make": "HONDA ACCORD",
                    "Location": "APO",
                    "Action": "Replacement",
                    "Area": "Windshield",
                    "Claim#": "Listed",
                    "WorkItem": "verified",
                },
                {
                    "Inventory Date": "03/09/2026",
                    "Original Date": "03/09/2026",
                    "MVA": "60853262",
                    "VIN": "JN8BT3DDXTW297427",
                    "Make": "NISSAN ROGUE AWD",
                    "Location": "APO",
                    "Action": "Repair",
                    "Area": "Windshield",
                    "Claim#": "Missing",
                    "WorkItem": "verified",
                },
            ]
        )

        sent_messages = []

        def fake_send(message):
            sent_messages.append(message)

        monkeypatch.setattr("GlassOrchestrator._send_email", fake_send)

        notify_order_items(df)

        assert len(sent_messages) == 1
        assert "(2 items)" in sent_messages[0].subject
        assert "59654641" in sent_messages[0].html_body
        assert "60853262" in sent_messages[0].html_body


# ─── UT-7: Location Extraction from Type Column ─────────────────────────────


class TestUT7_LocationExtraction:
    """Verify location suffix extraction from Type column (e.g., '0420APO' → 'APO')."""

    def test_apo_suffix(self):
        """Type column '0420APO' → Location 'APO'."""
        assert _extract_location_from_type("0420APO") == "APO"

    def test_bb_suffix(self):
        """Type column '0420BB' → Location 'BB'."""
        assert _extract_location_from_type("0420BB") == "BB"

    def test_lowercase_suffix_normalized(self):
        """Type column '0420apo' → Location 'APO' (case-insensitive)."""
        assert _extract_location_from_type("0420apo") == "APO"

    def test_mixed_case_bb(self):
        """Type column '0420Bb' → Location 'BB'."""
        assert _extract_location_from_type("0420Bb") == "BB"

    def test_no_suffix_uses_default(self):
        """Type column '0420' (no suffix) → falls back to default location."""
        result = _extract_location_from_type("0420")
        # Should return the config default location
        assert result == "APO"

    def test_empty_string_uses_default(self):
        """Empty Type column → falls back to default location."""
        result = _extract_location_from_type("")
        assert result == "APO"

    def test_none_value_uses_default(self):
        """None Type column → falls back to default location."""
        result = _extract_location_from_type(None)
        assert result == "APO"

    def test_whitespace_trimmed(self):
        """Type column ' 0420APO ' → Location 'APO' (whitespace trimmed)."""
        assert _extract_location_from_type(" 0420APO ") == "APO"

    def test_unknown_suffix_uses_default(self):
        """Type column '0420XYZ' (unknown suffix) → falls back to default."""
        result = _extract_location_from_type("0420XYZ")
        assert result == "APO"

    def test_manifest_uses_extracted_location(self):
        """parse_descriptions_to_manifest correctly extracts location from Type."""
        manifest, _ = parse_descriptions_to_manifest(
            [("0305BB", "59340120WS")], datetime(2026, 3, 5)
        )
        assert manifest["59340120"]["Location"] == "BB"

    def test_manifest_uses_type_mmdd_for_inventory_date(self):
        """Type '0502APO' should drive Inventory Date to 05/02/current year."""
        manifest, _ = parse_descriptions_to_manifest(
            [("0502APO", "59340120WS")], datetime(2026, 5, 1)
        )
        assert manifest["59340120"]["Inventory Date"] == "05/02/2026"
        assert manifest["59340120"]["Original Date"] == "05/02/2026"

    def test_manifest_type_invalid_date_falls_back_to_email_date(self):
        """Invalid Type MMDD (e.g. 0230) should fall back to email Date header day."""
        manifest, _ = parse_descriptions_to_manifest(
            [("0230APO", "59340120WS")], datetime(2026, 5, 1)
        )
        assert manifest["59340120"]["Inventory Date"] == "05/01/2026"

    def test_manifest_type_slash_date_is_rejected_and_falls_back(self):
        """Slash format is invalid; parser should fall back to email Date header day."""
        manifest, _ = parse_descriptions_to_manifest(
            [("05/02APO", "59340120WS")], datetime(2026, 5, 1)
        )
        assert manifest["59340120"]["Inventory Date"] == "05/01/2026"

    def test_manifest_type_letter_o_in_date_is_rejected_and_falls_back(self):
        """Strict MMDD requires digits; letter O typo should fall back to email date."""
        manifest, _ = parse_descriptions_to_manifest(
            [("05O2APO", "59340120WS")], datetime(2026, 5, 1)
        )
        assert manifest["59340120"]["Inventory Date"] == "05/01/2026"
