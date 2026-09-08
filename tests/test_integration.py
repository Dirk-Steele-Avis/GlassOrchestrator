"""
Integration Tests for GlassOrchestrator — System handshakes between components.

IT-1: Gmail Connection & Search  (requires live credentials — skipped by default)
IT-2: Worker Handoff (File Bridge)
IT-3: Merge Reconciliation
IT-4: Spreadsheet Persistence
IT-6: Glass Work Item Phase (driver mocked)
IT-7: All Area × Claim Combinations (mocked sheet)
IT-8: Live Sheet — All Area × Claim Combinations (real Google Sheet; opt-in via GLASS_RUN_LIVE_SHEETS_TESTS=1)
"""

import csv
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from GlassOrchestrator import (
    AREAS,
    COLUMNS,
    CSV_PATH,
    DATA_DIR,
    IMAP_SERVER,
    SERVICE_ACCOUNT_JSON,
    SHEETS_TIMEOUT,
    SPREADSHEET_ID,
    SHEET_NAME,
    TARGET_SENDER,
    _find_insert_row,
    _get_worksheet,
    parse_descriptions_to_manifest,
    merge_manifest_with_results,
    persist_new_rows,
)


_LIVE_SHEETS_OPT_IN = os.getenv("GLASS_RUN_LIVE_SHEETS_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
_HAS_SHEET_ID = SPREADSHEET_ID != "YOUR_SPREADSHEET_ID_HERE"
_HAS_SERVICE_ACCOUNT_FILE = Path(SERVICE_ACCOUNT_JSON).exists()
_RUN_LIVE_SHEETS_IT5 = _LIVE_SHEETS_OPT_IN and _HAS_SHEET_ID and _HAS_SERVICE_ACCOUNT_FILE


# ─── IT-1: Gmail Connection & Search ─────────────────────────────────────────


@pytest.mark.skipif(
    not os.getenv("GLASS_EMAIL_ACCOUNT") or not os.getenv("GLASS_EMAIL_PASSWORD"),
    reason="Gmail credentials not configured (set GLASS_EMAIL_ACCOUNT / GLASS_EMAIL_PASSWORD)",
)
class TestIT1_GmailConnection:
    """Verify IMAP authentication and UNSEEN message search."""

    def test_imap_auth_and_search(self):
        import imaplib

        account = os.getenv("GLASS_EMAIL_ACCOUNT")
        password = os.getenv("GLASS_EMAIL_PASSWORD")

        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        try:
            mail.login(account, password)
            mail.select("inbox")
            status, msg_ids = mail.search(None, f'(FROM "{TARGET_SENDER}")')
            assert status == "OK"
            # msg_ids[0] may be empty (no messages) — that's still OK
            count = len(msg_ids[0].split()) if msg_ids[0] else 0
            assert isinstance(count, int)
        finally:
            mail.logout()


# ─── IT-2: Worker Handoff (File Bridge) ──────────────────────────────────────


class TestIT2_WorkerHandoff:
    """Verify CSV formatting and subprocess launch capability."""

    def test_csv_written_correctly(self, tmp_path):
        """Write MVAs to a CSV and validate structure."""
        csv_path = tmp_path / "GlassDataParser.csv"
        mva_list = ["59340120", "59340121", "59340122"]

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["MVA"])
            for mva in mva_list:
                writer.writerow([mva])

        # Validate
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert "MVA" in reader.fieldnames
            rows = list(reader)
            assert len(rows) == 3
            assert rows[0]["MVA"] == "59340120"
            assert rows[2]["MVA"] == "59340122"

    def test_subprocess_can_launch_python(self):
        """Verify subprocess.check_call can invoke Python successfully."""
        # Run a trivial Python command to prove the subprocess mechanism works
        subprocess.check_call(
            [sys.executable, "-c", "print('worker ok')"],
        )

    def test_subprocess_detects_failure(self):
        """Verify subprocess.CalledProcessError is raised on non-zero exit."""
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.check_call(
                [sys.executable, "-c", "import sys; sys.exit(1)"],
            )


# ─── IT-3: Merge Reconciliation ──────────────────────────────────────────────


class TestIT3_MergeReconciliation:
    """Verify left-join logic: missing MVAs produce VIN='N/A', no rows dropped."""

    def test_left_join_with_missing_mva(self, tmp_path, monkeypatch):
        """One MVA present in results, one missing → VIN=N/A for missing."""
        # Build manifest from parsing step
        descriptions = [("0305APO", "59340120WS"), ("0305APO", "59340121WS")]
        manifest, _ = parse_descriptions_to_manifest(descriptions, datetime(2026, 3, 5))

        # Create a mock GlassResults.txt with only ONE of the two MVAs
        results_file = tmp_path / "GlassResults.txt"
        results_file.write_text("MVA,VIN,Desc\n59340120,1HGCM82633A004352,Windshield\n")

        # Patch the RESULTS_PATH used by merge step
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", results_file)

        df = merge_manifest_with_results(manifest)

        assert len(df) == 2  # Both MVAs must be present (no drop)
        row_120 = df[df["MVA"] == "59340120"].iloc[0]
        row_121 = df[df["MVA"] == "59340121"].iloc[0]

        assert row_120["VIN"] == "1HGCM82633A004352"
        assert row_121["VIN"] == "N/A"  # Missing from scraper → N/A

    def test_all_mvas_missing_from_results(self, tmp_path, monkeypatch):
        """No scraper results at all → all VINs become N/A."""
        descriptions = [("0305APO", "59340120WS"), ("0305APO", "59340121WS")]
        manifest, _ = parse_descriptions_to_manifest(descriptions, datetime(2026, 3, 5))

        results_file = tmp_path / "GlassResults.txt"
        results_file.write_text("MVA,VIN,Desc\n")  # header only

        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", results_file)

        df = merge_manifest_with_results(manifest)

        assert len(df) == 2
        assert (df["VIN"] == "N/A").all()

    def test_results_file_missing(self, tmp_path, monkeypatch):
        """No GlassResults.txt file → degrade gracefully, all VINs = N/A."""
        descriptions = [("0305APO", "59340120WS")]
        manifest, _ = parse_descriptions_to_manifest(descriptions, datetime(2026, 3, 5))

        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")

        df = merge_manifest_with_results(manifest)
        assert len(df) == 1
        assert df.iloc[0]["VIN"] == "N/A"


# ─── IT-4: Spreadsheet Persistence ───────────────────────────────────────────


class TestIT4_SpreadsheetPersistence:
    """Verify data is appended to Google Sheet without overwriting."""

    @patch("GlassOrchestrator.gspread")
    def test_sheet_client_sets_request_timeout(self, mock_gspread):
        client = mock_gspread.service_account.return_value
        worksheet = client.open_by_key.return_value.worksheet.return_value

        assert _get_worksheet() is worksheet
        client.set_timeout.assert_called_once_with(SHEETS_TIMEOUT)
        client.open_by_key.assert_called_once_with(SPREADSHEET_ID)

    def _make_test_df(self, mvas, date="03/05/2026"):
        rows = []
        for mva in mvas:
            rows.append({
                "Inventory Date": date,
                "Original Date": date,
                "MVA": mva,
                "FPO#": "",
                "VIN": "1HGCM82633A004352",
                "Make": "Windshield",
                "Location": "APO",
                "Action": "Replacement",
                "Area": "Windshield",
                "Claim#": "Missing",
                "WorkItem": "verified",
            })
        return pd.DataFrame(rows, columns=COLUMNS)

    def _mock_worksheet(self, existing_rows=None):
        """Create a mock worksheet with optional existing data."""
        ws = MagicMock()
        ws.id = 123
        header = ["Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make", "Location",
                  "Action", "Area", "Claim#", "WorkItem"]
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]
        if existing_rows is None:
            existing_rows = []
        ws.get_all_values.return_value = [header] + existing_rows + [summary]
        return ws

    def test_insert_row_uses_last_mva_before_summary(self):
        """Blank separator rows do not move insertion below the summary."""
        header = ["Inventory Date", "Original Date", "MVA", "FPO#", "VIN", "Make", "Location",
                  "Action", "Area", "Claim#", "WorkItem"]
        data = ["08/26/2026", "08/26/2026", "61093642", "", "VIN", "Make", "BB",
                "Replace(AGN)", "Windshield", "Missing", "verified"]
        blank = [""] * len(header)
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]

        assert _find_insert_row([header, data, blank, blank, summary]) == 3

    def test_insert_row_uses_configured_mva_column_with_blank_sheet_header(self):
        header = [""] * len(COLUMNS)
        data = ["08/26/2026", "08/26/2026", "61093642"]
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]

        assert _find_insert_row([header, data, summary]) == 3

    def test_insert_row_requires_summary(self):
        with pytest.raises(RuntimeError, match="found 0 matching rows"):
            _find_insert_row([list(COLUMNS)])

    def test_insert_row_rejects_duplicate_summaries(self):
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]

        with pytest.raises(RuntimeError, match="found 2 matching rows"):
            _find_insert_row([list(COLUMNS), summary, summary])

    def test_insert_row_empty_table_uses_row_two(self):
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]

        assert _find_insert_row([list(COLUMNS), summary]) == 2

    @patch("GlassOrchestrator._get_worksheet")
    def test_creates_new_rows(self, mock_get_ws):
        """First write updates cells in the sheet."""
        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120", "59340121"])
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 2
        ws.spreadsheet.batch_update.assert_called_once()
        insert_request = ws.spreadsheet.batch_update.call_args.args[0]["requests"][0]["insertDimension"]
        assert insert_request == {
            "range": {
                "sheetId": 123,
                "dimension": "ROWS",
                "startIndex": 1,
                "endIndex": 3,
            },
            "inheritFromBefore": True,
        }
        written = ws.update.call_args.args[0]
        assert ws.update.call_args.kwargs["range_name"] == "A2:K3"
        assert len(written) == 2
        assert written[0][2] == "59340120"
        assert written[1][2] == "59340121"

    @patch("GlassOrchestrator._get_worksheet")
    def test_appends_without_overwriting(self, mock_get_ws):
        """Second write appends new rows; existing data untouched."""
        existing = [["03/05/2026", "03/05/2026", "59340120", "", "1HGCM82633A004352",
                      "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340121"])
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 1
        ws.spreadsheet.batch_update.assert_called_once()
        written = ws.update.call_args.args[0]
        assert ws.update.call_args.kwargs["range_name"] == "A3:K3"
        assert written[0][2] == "59340121"

    @patch("GlassOrchestrator._get_worksheet")
    def test_existing_row_does_not_block_write(self, mock_get_ws):
        """Same MVA/date already in sheet still results in a newly inserted row."""
        existing = [["03/05/2026", "03/05/2026", "59340120", "", "1HGCM82633A004352",
                      "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"])
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 1
        ws.update.assert_called_once()

    @patch("GlassOrchestrator._get_worksheet")
    def test_existing_row_with_different_date_format_still_writes(self, mock_get_ws):
        """Date-format differences do not suppress inserts when dedupe is disabled."""
        existing = [["5/2/2026", "5/2/2026", "59193750", "", "KNDPU3DG9T7301521",
                     "KIA SPORTAGE 2WD", "APO", "Replace(AGN)", "Windshield", "Listed", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59193750"], date="05/02/2026")
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 1
        ws.update.assert_called_once()

    @patch("GlassOrchestrator._get_worksheet")
    def test_correct_columns_written(self, mock_get_ws):
        """Verify all columns match the expected schema."""
        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"])
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        assert written[0] == ["03/05/2026", "03/05/2026", "59340120", "", "1HGCM82633A004352",
                                "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]

    @patch("GlassOrchestrator._get_worksheet")
    def test_same_lifecycle_still_inserts_new_row(self, mock_get_ws):
        """Same-MVA sighting inside window inserts a new row when dedupe is disabled."""
        existing = [["03/05/2026", "03/05/2026", "59340120", "", "1HGCM82633A004352",
                     "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"], date="03/07/2026")
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 1
        ws.update.assert_called_once()
        ws.update_cell.assert_not_called()

    @patch("GlassOrchestrator._get_worksheet")
    def test_new_lifecycle_inserts_new_row_after_window(self, mock_get_ws):
        """Same MVA outside lifecycle window should start a new incident row."""
        existing = [["03/01/2026", "03/01/2026", "59340120", "", "1HGCM82633A004352",
                     "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"], date="03/06/2026")
        new_rows = persist_new_rows(df)

        assert len(new_rows) == 1
        ws.update.assert_called_once()

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_carried_forward_from_sheet(self, mock_get_ws):
        """When an MVA already has a row in the sheet, new row inherits the earliest Original Date."""
        existing = [["03/05/2026", "03/04/2026", "59340120", "", "1HGCM82633A004352",
                     "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        # New row 2 days after existing Inventory Date (within 7-day window)
        df = self._make_test_df(["59340120"], date="03/07/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        inventory_col = COLUMNS.index("Inventory Date")
        original_col = COLUMNS.index("Original Date")
        assert written[0][inventory_col] == "03/07/2026"        # current date
        assert written[0][original_col] == "03/04/2026"         # earliest prior Original Date

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_uses_earliest_when_multiple_prior_rows(self, mock_get_ws):
        """Earliest Original Date is used when the sheet has multiple rows for the same MVA."""
        existing = [
            ["03/06/2026", "03/04/2026", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
            ["03/05/2026", "03/03/2026", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
        ]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        # New row 2-3 days after existing rows — both within the window
        df = self._make_test_df(["59340120"], date="03/08/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        original_col = COLUMNS.index("Original Date")
        assert written[0][original_col] == "03/03/2026"         # earliest of 03/03 and 03/04

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_unchanged_for_first_arrival(self, mock_get_ws):
        """When the MVA has no prior rows in the sheet, Original Date stays as Inventory Date."""
        ws = self._mock_worksheet()         # empty sheet
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59999999"], date="03/07/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        inventory_col = COLUMNS.index("Inventory Date")
        original_col = COLUMNS.index("Original Date")
        assert written[0][inventory_col] == "03/07/2026"
        assert written[0][original_col] == "03/07/2026"         # first arrival — no prior date

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_outside_window_not_carried_forward(self, mock_get_ws):
        """Prior rows outside the incident window (>7 days) are ignored for Original Date lookup."""
        existing = [["03/01/2026", "02/28/2026", "59340120", "", "1HGCM82633A004352",
                     "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        # An 8-day date difference means seven intervening days without the MVA.
        df = self._make_test_df(["59340120"], date="03/09/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        original_col = COLUMNS.index("Original Date")
        assert written[0][original_col] == "03/09/2026"         # not carried forward

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_walks_back_through_latest_episode(self, mock_get_ws):
        """Adjacent sightings chain backward even when the full episode exceeds seven days."""
        existing = [
            ["03/01/2026", "03/01/2026", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
            ["03/07/2026", "03/01/2026", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
            ["03/13/2026", "03/01/2026", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
        ]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"], date="03/20/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        original_col = COLUMNS.index("Original Date")
        assert written[0][original_col] == "03/01/2026"

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_excludes_old_episode(self, mock_get_ws):
        """An old sighting and stale Original Date do not leak into the latest episode."""
        existing = [
            ["09/01/2025", "09/01/2025", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
            ["03/17/2026", "09/01/2025", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
            ["03/19/2026", "09/01/2025", "59340120", "", "1HGCM82633A004352",
             "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"],
        ]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        df = self._make_test_df(["59340120"], date="03/20/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        original_col = COLUMNS.index("Original Date")
        assert written[0][original_col] == "03/17/2026"

    @patch("GlassOrchestrator._get_worksheet")
    def test_original_date_legacy_blank_falls_back_to_inventory_date(self, mock_get_ws):
        """Legacy rows with blank Original Date should contribute their Inventory Date."""
        existing = [["03/06/2026", "", "59340120", "", "1HGCM82633A004352",
                     "Windshield", "APO", "Replace(AGN)", "Windshield", "Missing", "verified"]]
        ws = self._mock_worksheet(existing)
        mock_get_ws.return_value = ws

        # Within the 7-day incident window; blank Original Date should resolve to 03/06/2026.
        df = self._make_test_df(["59340120"], date="03/07/2026")
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        original_col = COLUMNS.index("Original Date")
        assert written[0][original_col] == "03/06/2026"


# ─── IT-5: Spreadsheet Configuration Health ──────────────────────────────────


@pytest.mark.skipif(
    not _RUN_LIVE_SHEETS_IT5,
    reason=(
        "Skipping live sheet health test. Set GLASS_RUN_LIVE_SHEETS_TESTS=1 and "
        "provide non-placeholder SPREADSHEET_ID with existing service account json."
    ),
)
class TestIT5_SpreadsheetConfigurationHealth:
    """Integration checks for spreadsheet configuration and accessibility."""

    def test_spreadsheet_id_is_not_placeholder(self):
        """Configured spreadsheet id should not be the default placeholder."""
        assert SPREADSHEET_ID != "YOUR_SPREADSHEET_ID_HERE", (
            "SPREADSHEET_ID is still placeholder. Set GLASS_SPREADSHEET_ID "
            "or update orchestrator_config.json."
        )

    def test_service_account_can_open_configured_sheet(self):
        """Service account should open configured sheet/tab successfully."""
        ws = _get_worksheet()
        assert ws is not None
        assert ws.title == SHEET_NAME


# ─── IT-6: Glass Work Item Phase (driver mocked) ─────────────────────────────


class TestIT6_GlassWorkItemPhase:
    """
    Integration tests for run_glass_work_item_phase().
    Driver is mocked; tests verify full call path through check → create → sheet update.
    """

    def _make_handler(self, status: str = "created", mva: str = "11111111") -> MagicMock:
        handler = MagicMock()
        handler.create_work_item.return_value = {"status": status, "mva": mva}
        return handler

    def _make_sheet_client(self) -> MagicMock:
        return MagicMock()

    def test_no_existing_item_calls_create_with_correct_config(self):
        """
        Full path: check returns False → create_work_item called with correct
        WorkItemConfig (mva, damage_type, location all matching manifest entry).
        """
        from flows.glass_work_item_phase import run_glass_work_item_phase

        driver = MagicMock()
        mock_handler = self._make_handler(status="created", mva="11111111")

        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler", return_value=mock_handler):
            run_glass_work_item_phase(
                driver,
                [{"mva": "11111111", "damage_type": "Replacement", "location": "WINDSHIELD"}],
            )

        mock_handler.create_work_item.assert_called_once()
        config = mock_handler.create_work_item.call_args[0][0]
        assert config.mva == "11111111"
        assert config.damage_type == "REPLACEMENT"   # WorkItemConfig normalizes to uppercase
        assert config.location == "WINDSHIELD"

    def test_two_mva_manifest_one_skips_one_creates(self):
        """
        Two-MVA manifest: first has existing item (skipped), second does not (created).
        Both are attempted; counts reflect actual outcomes.
        """
        from flows.glass_work_item_phase import run_glass_work_item_phase

        driver = MagicMock()
        mock_handler = self._make_handler(status="created", mva="22222222")
        check_results = [True, False]  # first MVA skipped, second created

        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item",
                   side_effect=check_results), \
             patch("flows.glass_work_item_phase.create_work_item_handler",
                   return_value=mock_handler):
            result = run_glass_work_item_phase(
                driver,
                [
                    {"mva": "11111111", "damage_type": "Replacement", "location": "WINDSHIELD"},
                    {"mva": "22222222", "damage_type": "Replacement", "location": "WINDSHIELD"},
                ],
            )

        assert result["processed"] == 2
        assert result["skipped"] == 1
        assert result["created"] == 1
        assert result["failed"] == 0

    def test_create_exception_does_not_propagate(self):
        """
        Exception raised inside create_work_item() must not propagate out of
        run_glass_work_item_phase() — loop continues and failed count increments.
        """
        from flows.glass_work_item_phase import run_glass_work_item_phase

        driver = MagicMock()
        mock_handler = MagicMock()
        mock_handler.create_work_item.side_effect = Exception("Compass timeout")

        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler",
                   return_value=mock_handler):
            result = run_glass_work_item_phase(
                driver,
                [{"mva": "11111111", "damage_type": "Replacement", "location": "WINDSHIELD"}],
            )

        assert result["failed"] == 1
        assert result["processed"] == 1

    def test_eligibility_consistent_between_phase6_and_phase7(self):
        """
        is_notification_eligible() used by both Phase 6 and Phase 7 must behave
        identically for the same input — Replacement eligible, Repair not.
        """
        from core.eligibility import is_notification_eligible

        replacement_row_phase6 = {"damage_type": "Replacement"}
        replacement_row_phase7 = {"Damage Type": "Replacement"}
        repair_row_phase6 = {"damage_type": "Repair"}
        repair_row_phase7 = {"Damage Type": "Repair"}

        assert is_notification_eligible(replacement_row_phase6) is True
        assert is_notification_eligible(replacement_row_phase7) is True
        assert is_notification_eligible(repair_row_phase6) is False
        assert is_notification_eligible(repair_row_phase7) is False

    def test_work_item_created_column_updated_in_sheet_after_success(self):
        """
        After successful creation, sheet_client.mark_work_item_created() is called
        with the correct MVA and tab_name.
        """
        from flows.glass_work_item_phase import run_glass_work_item_phase

        driver = MagicMock()
        mock_handler = self._make_handler(status="created", mva="11111111")
        mock_sheet = self._make_sheet_client()

        with patch("flows.glass_work_item_phase.warmup_compass", return_value=True), \
             patch("flows.glass_work_item_phase.navigate_to_mva", return_value=True), \
             patch("flows.glass_work_item_phase.check_existing_work_item", return_value=False), \
             patch("flows.glass_work_item_phase.create_work_item_handler",
                   return_value=mock_handler):
            run_glass_work_item_phase(
                driver,
                [{"mva": "11111111", "damage_type": "Replacement", "location": "WINDSHIELD"}],
                sheet_client=mock_sheet,
                tab_name="GlassClaims",
            )

        mock_sheet.mark_work_item_created.assert_called_once_with("11111111", "GlassClaims")


# ─── IT-7: All Area × Claim Combinations ─────────────────────────────────────

# All valid (scan_string, expected_action, expected_area, expected_claim) tuples.
# Repair is only valid for WS; all other areas produce Replacement only.
_ALL_SCAN_CASES = [
    ("60000001WS",   "Replacement", "Windshield",          "Missing"),
    ("60000002WSc",  "Replacement", "Windshield",          "Listed"),
    ("60000003WSr",  "Repair",      "Windshield",          "Missing"),
    ("60000004WSrc", "Repair",      "Windshield",          "Listed"),
    ("60000005LFD",  "Replacement", "Left Front Door",     "Missing"),
    ("60000006LFDc", "Replacement", "Left Front Door",     "Listed"),
    ("60000007RFD",  "Replacement", "Right Front Door",    "Missing"),
    ("60000008RFDc", "Replacement", "Right Front Door",    "Listed"),
    ("60000009LRD",  "Replacement", "Left Rear Door",      "Missing"),
    ("60000010LRDc", "Replacement", "Left Rear Door",      "Listed"),
    ("60000011RRD",  "Replacement", "Right Rear Door",     "Missing"),
    ("60000012RRDc", "Replacement", "Right Rear Door",     "Listed"),
    ("60000013LFV",  "Replacement", "Left Front Vent",     "Missing"),
    ("60000014LFVc", "Replacement", "Left Front Vent",     "Listed"),
    ("60000015RFV",  "Replacement", "Right Front Vent",    "Missing"),
    ("60000016RFVc", "Replacement", "Right Front Vent",    "Listed"),
    ("60000017BW",   "Replacement", "Back Window",         "Missing"),
    ("60000018BWc",  "Replacement", "Back Window",         "Listed"),
    ("60000019SR",   "Replacement", "Sunroof",             "Missing"),
    ("60000020SRc",  "Replacement", "Sunroof",             "Listed"),
    ("60000021TFS",  "Replacement", "Sunroof",             "Missing"),
    ("60000022TFSc", "Replacement", "Sunroof",             "Listed"),
    ("60000023TRS",  "Replacement", "Sunroof",             "Missing"),
    ("60000024TRSc", "Replacement", "Sunroof",             "Listed"),
    ("60000025LRQ",  "Replacement", "Left Rear Quarter",   "Missing"),
    ("60000026LRQc", "Replacement", "Left Rear Quarter",   "Listed"),
    ("60000027RRQ",  "Replacement", "Right Rear Quarter",  "Missing"),
    ("60000028RRQc", "Replacement", "Right Rear Quarter",  "Listed"),
    ("60000029RFW",  "Replacement", "Right Front Window",  "Missing"),
    ("60000030RFWc", "Replacement", "Right Front Window",  "Listed"),
    ("60000031RVM",  "Replacement", "Rear View Mirror",    "Missing"),
    ("60000032RVMc", "Replacement", "Rear View Mirror",    "Listed"),
    ("60000033CAM",  "Replacement", "Camera",              "Missing"),
    ("60000034CAMc", "Replacement", "Camera",              "Listed"),
]


class TestIT7_AllAreaClaimCombinations:
    """
    Full pipeline walkthrough for all valid AREA_ID × claim-flag combinations.

    Simulates an Orca Scan email containing 34 scan strings (one per valid combo),
    then drives parse → merge → persist and asserts correctness at each stage.

    Repair is only valid for WS; all 15 other areas produce Replacement only.
    Total combinations: WS(4) + non-WS x 15 areas x 2 claim flags = 34.
    """

    _EMAIL_DATE = datetime(2026, 4, 25)
    _TYPE_VALUE = "0425APO"   # → Location = "APO"

    def _all_descriptions(self):
        return [(self._TYPE_VALUE, scan) for scan, *_ in _ALL_SCAN_CASES]

    def _parse_all(self):
        return parse_descriptions_to_manifest(self._all_descriptions(), self._EMAIL_DATE)

    def _mock_worksheet(self):
        ws = MagicMock()
        ws.id = 123
        summary = ["", "Avg Repair Days", "", "", "", "", "", "Repairs", "", "Claims", "Total"]
        ws.get_all_values.return_value = [list(COLUMNS), summary]
        return ws

    # ── Parse stage ───────────────────────────────────────────────────────────

    def test_all_34_combos_present_in_manifest(self):
        """All 34 valid scan strings produce a manifest entry."""
        manifest, mva_list = self._parse_all()
        assert len(manifest) == 34
        assert len(mva_list) == 34

    @pytest.mark.parametrize(
        "scan,expected_action,expected_area,expected_claim", _ALL_SCAN_CASES
    )
    def test_each_combo_parses_to_correct_values(
        self, scan, expected_action, expected_area, expected_claim
    ):
        """Each scan string resolves to the correct Action, Area, and Claim#."""
        mva = scan[:8]
        manifest, mva_list = parse_descriptions_to_manifest(
            [(self._TYPE_VALUE, scan)], self._EMAIL_DATE
        )
        assert mva in manifest, f"MVA {mva} not found in manifest for scan '{scan}'"
        row = manifest[mva]
        assert row["Action"] == expected_action
        assert row["Area"] == expected_area
        assert row["Claim#"] == expected_claim
        assert mva in mva_list

    def test_repair_only_on_windshield(self):
        """No non-WS area produces a Repair action."""
        manifest, _ = self._parse_all()
        for mva, row in manifest.items():
            if row["Area"] != "Windshield":
                assert row["Action"] == "Replacement", (
                    f"MVA {mva}: area='{row['Area']}' produced action='{row['Action']}'"
                )

    def test_all_ws_action_variants_present(self):
        """WS produces all 4 variants: Repair×Missing, Repair×Listed, Replacement×Missing, Replacement×Listed."""
        manifest, _ = self._parse_all()
        ws_rows = [r for r in manifest.values() if r["Area"] == "Windshield"]
        assert len(ws_rows) == 4
        combos = {(r["Action"], r["Claim#"]) for r in ws_rows}
        assert combos == {
            ("Replacement", "Missing"),
            ("Replacement", "Listed"),
            ("Repair",      "Missing"),
            ("Repair",      "Listed"),
        }

    def test_location_extracted_for_all_rows(self):
        """All 34 rows inherit location from the email type value."""
        manifest, _ = self._parse_all()
        for mva, row in manifest.items():
            assert row["Location"] == "APO", f"MVA {mva} has unexpected location '{row['Location']}'"

    # ── Merge stage ───────────────────────────────────────────────────────────

    def test_merge_produces_34_rows_all_vin_na(self, tmp_path, monkeypatch):
        """All 34 manifest rows survive merge; VIN='N/A' when scraper results absent."""
        manifest, _ = self._parse_all()
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        df = merge_manifest_with_results(manifest)
        assert len(df) == 34
        assert (df["VIN"] == "N/A").all()

    # ── Persist stage ─────────────────────────────────────────────────────────

    @patch("GlassOrchestrator._get_worksheet")
    def test_all_34_rows_written_to_sheet(self, mock_get_ws, tmp_path, monkeypatch):
        """persist_new_rows() writes exactly 34 rows to an empty sheet."""
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        manifest, _ = self._parse_all()
        df = merge_manifest_with_results(manifest)

        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws

        new_rows = persist_new_rows(df)

        assert len(new_rows) == 34
        ws.update.assert_called_once()
        written = ws.update.call_args.args[0]
        assert len(written) == 34

    @patch("GlassOrchestrator._get_worksheet")
    def test_vendor_labels_applied_in_sheet_rows(self, mock_get_ws, tmp_path, monkeypatch):
        """Repair→'Repair(SuperGlass)', Replacement→'Replace(AGN)' in written rows; internal labels absent."""
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        manifest, _ = self._parse_all()
        df = merge_manifest_with_results(manifest)

        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        action_col_idx = list(COLUMNS).index("Action")
        written_actions = {row[action_col_idx] for row in written}

        assert "Repair(SuperGlass)" in written_actions
        assert "Replace(AGN)" in written_actions
        assert "Repair" not in written_actions,       "Internal label 'Repair' must not reach the sheet"
        assert "Replacement" not in written_actions,  "Internal label 'Replacement' must not reach the sheet"

    @patch("GlassOrchestrator._get_worksheet")
    def test_oem_replacement_routes_to_avis(self, mock_get_ws, tmp_path, monkeypatch):
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        manifest, _ = parse_descriptions_to_manifest(
            [
                (self._TYPE_VALUE, "60000100WS"),
                (self._TYPE_VALUE, "60000101WS OEM"),
            ],
            self._EMAIL_DATE,
        )
        df = merge_manifest_with_results(manifest)

        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        action_col_idx = list(COLUMNS).index("Action")
        area_col_idx = list(COLUMNS).index("Area")
        assert written[0][action_col_idx] == "Replace(AGN)"
        assert written[1][action_col_idx] == "Replace(AVIS)"
        assert written[0][area_col_idx] == "Windshield"
        assert written[1][area_col_idx] == "Windshield(OEM)"

    @patch("GlassOrchestrator._get_worksheet")
    def test_every_area_supports_both_oem_claim_states(self, mock_get_ws, tmp_path, monkeypatch):
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        descriptions = []
        expected = []
        for index, (area_code, area_label) in enumerate(AREAS.items(), start=1):
            missing_mva = f"61{index:06d}"
            listed_mva = f"62{index:06d}"
            descriptions.extend([
                (self._TYPE_VALUE, f"{missing_mva}{area_code} OEM"),
                (self._TYPE_VALUE, f"{listed_mva}{area_code}c OEM"),
            ])
            expected.extend([
                (f"{area_label}(OEM)", "Missing"),
                (f"{area_label}(OEM)", "Listed"),
            ])

        manifest, _ = parse_descriptions_to_manifest(descriptions, self._EMAIL_DATE)
        df = merge_manifest_with_results(manifest)
        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        action_col_idx = list(COLUMNS).index("Action")
        area_col_idx = list(COLUMNS).index("Area")
        claim_col_idx = list(COLUMNS).index("Claim#")
        assert len(written) == len(AREAS) * 2
        assert [
            (row[area_col_idx], row[claim_col_idx]) for row in written
        ] == expected
        assert {row[action_col_idx] for row in written} == {"Replace(AVIS)"}

    @patch("GlassOrchestrator._get_worksheet")
    def test_claim_listed_and_missing_split_evenly(self, mock_get_ws, tmp_path, monkeypatch):
        """Listed and Missing rows remain evenly split across all combinations."""
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", tmp_path / "nonexistent.txt")
        manifest, _ = self._parse_all()
        df = merge_manifest_with_results(manifest)

        ws = self._mock_worksheet()
        mock_get_ws.return_value = ws
        persist_new_rows(df)

        written = ws.update.call_args.args[0]
        claim_col_idx = list(COLUMNS).index("Claim#")
        listed_count  = sum(1 for row in written if row[claim_col_idx] == "Listed")
        missing_count = sum(1 for row in written if row[claim_col_idx] == "Missing")

        expected_count = len(_ALL_SCAN_CASES) // 2
        assert listed_count == expected_count
        assert missing_count == expected_count


# ─── IT-8: Live Sheet — All Area × Claim Combinations ────────────────────────

_IT8_SENTINEL_DATE = "01/01/2099"   # Far-future date; never matches real claims


def _it8_delete_sentinel_rows(ws) -> int:
    """
    Remove any rows whose Make column is 'IT8_TEST' (the sentinel marker).
    Deletes bottom-to-top to avoid index drift.
    Returns the count of rows deleted.
    """
    all_vals = ws.get_all_values()
    if not all_vals:
        return 0

    headers = all_vals[0]
    try:
        make_idx = headers.index("Make")
    except ValueError:
        return 0

    indices = [
        i + 1   # 1-based sheet row number
        for i, row in enumerate(all_vals[1:], start=1)
        if len(row) > make_idx and row[make_idx].strip() == "IT8_TEST"
    ]
    for row_idx in sorted(indices, reverse=True):
        ws.delete_rows(row_idx)
    return len(indices)


@pytest.fixture(scope="class")
def live_sheet_it8(tmp_path_factory):
    """
    Class-scoped fixture for IT-8.

    Setup  — purges any leftover sentinel rows, then writes all 26 test rows
             to the real GlassClaims sheet using the production persist_new_rows().
    Yield  — (ws, written_df) for tests to inspect.
    Teardown — purges sentinel rows UNLESS GLASS_IT8_SKIP_TEARDOWN=1, which
               leaves them in the sheet so you can inspect them manually.
    """
    ws = _get_worksheet()

    # Pre-clean: remove leftovers from a previously interrupted run
    _it8_delete_sentinel_rows(ws)

    # Build a DataFrame with all 26 combinations using the sentinel date
    rows = []
    for scan, expected_action, expected_area, expected_claim in _ALL_SCAN_CASES:
        rows.append({
            "Inventory Date": _IT8_SENTINEL_DATE,
            "Original Date": _IT8_SENTINEL_DATE,
            "MVA":          scan[:8],
            "FPO#":         "",
            "VIN":          "N/A",
            "Make":         "IT8_TEST",
            "Location":     "APO",
            "Action":       expected_action,
            "Area":         expected_area,
            "Claim#":       expected_claim,
            "WorkItem":     "verified",
        })
    df = pd.DataFrame(rows, columns=COLUMNS)

    written = persist_new_rows(df)

    yield ws, written

    # Teardown: skip if GLASS_IT8_SKIP_TEARDOWN=1 so rows stay visible in sheet
    skip_teardown = os.getenv("GLASS_IT8_SKIP_TEARDOWN", "").strip() == "1"
    if skip_teardown:
        print("\n[IT-8] Teardown skipped (GLASS_IT8_SKIP_TEARDOWN=1) — sentinel rows left in sheet for inspection.")
    else:
        _it8_delete_sentinel_rows(ws)


@pytest.mark.skipif(
    not _RUN_LIVE_SHEETS_IT5,
    reason=(
        "Skipping live sheet IT-8 tests. Set GLASS_RUN_LIVE_SHEETS_TESTS=1 and "
        "provide non-placeholder SPREADSHEET_ID with existing service account json."
    ),
)
class TestIT8_LiveSheetAllCombinations:
    """
    Live end-to-end test: writes all 26 valid area × claim combinations to the
    real GlassClaims Google Sheet and reads back to validate correctness.

    Requires GLASS_RUN_LIVE_SHEETS_TESTS=1 and a valid Service_account.json.
    All rows use Inventory Date='01/01/2099' so they can never collide with real
    claims.  The fixture tears down (deletes sentinel rows) after every run.
    """

    def test_26_rows_written(self, live_sheet_it8):
        """persist_new_rows() reports 26 new rows written."""
        _, written = live_sheet_it8
        assert len(written) == 26, (
            f"Expected 26 rows written, got {len(written)}.  "
            "Check whether sentinel rows already existed (duplicate guard)."
        )

    def test_sentinel_rows_visible_in_sheet(self, live_sheet_it8):
        """All 26 sentinel rows are readable back from the live sheet."""
        ws, _ = live_sheet_it8
        sentinel_mvas = {scan[:8] for scan, *_ in _ALL_SCAN_CASES}

        all_vals = ws.get_all_values()
        headers  = all_vals[0]
        mva_idx  = headers.index("MVA")
        make_idx = headers.index("Make")

        found_mvas = {
            row[mva_idx].strip()
            for row in all_vals[1:]
            if (
                len(row) > max(mva_idx, make_idx)
                and row[mva_idx].strip() in sentinel_mvas
                and row[make_idx].strip() == "IT8_TEST"
            )
        }
        assert found_mvas == sentinel_mvas, (
            f"Missing from sheet: {sentinel_mvas - found_mvas}"
        )

    @pytest.mark.parametrize(
        "scan,expected_action,expected_area,expected_claim", _ALL_SCAN_CASES
    )
    def test_each_row_has_correct_values(self, live_sheet_it8, scan, expected_action, expected_area, expected_claim):
        """
        Each of the 26 rows in the live sheet has the correct vendor-labelled
        Action, the correct Area, and the correct Claim# value.
        """
        ws, _ = live_sheet_it8
        mva = scan[:8]

        all_vals = ws.get_all_values()
        headers  = all_vals[0]
        mva_idx    = headers.index("MVA")
        make_idx   = headers.index("Make")
        action_idx = headers.index("Action")
        area_idx   = headers.index("Area")
        claim_idx  = headers.index("Claim#")

        matching = [
            row for row in all_vals[1:]
            if (
                len(row) > max(mva_idx, make_idx, action_idx, area_idx, claim_idx)
                and row[mva_idx].strip() == mva
                and row[make_idx].strip() == "IT8_TEST"
            )
        ]
        assert len(matching) == 1, f"Expected exactly 1 sheet row for MVA {mva}, found {len(matching)}"
        row = matching[0]

        # Action column holds the vendor label in the sheet
        expected_vendor_action = (
            "Repair(SuperGlass)" if expected_action == "Repair" else "Replace(AGN)"
        )
        assert row[action_idx].strip() == expected_vendor_action, (
            f"MVA {mva}: Action expected '{expected_vendor_action}', got '{row[action_idx]}'"
        )
        assert row[area_idx].strip()  == expected_area,  f"MVA {mva}: Area mismatch"
        assert row[claim_idx].strip() == expected_claim, f"MVA {mva}: Claim# mismatch"

    def test_sentinel_row_count_in_sheet(self, live_sheet_it8):
        """
        Exactly 26 sentinel rows are present in the live sheet after the write.
        Non-destructive — does not delete any rows.
        """
        ws, _ = live_sheet_it8

        all_vals = ws.get_all_values()
        headers  = all_vals[0]
        make_idx = headers.index("Make")

        sentinel_rows = [
            row for row in all_vals[1:]
            if len(row) > make_idx and row[make_idx].strip() == "IT8_TEST"
        ]
        assert len(sentinel_rows) == 26, (
            f"Expected 26 sentinel rows in sheet, found {len(sentinel_rows)}"
        )
