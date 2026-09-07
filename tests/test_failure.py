"""
Failure & Recovery Tests for GlassOrchestrator — Edge cases.

FT-1: Worker Crash Handling
FT-2: Stale Results Protection
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from GlassOrchestrator import (
    run_pipeline,
    validate_results_freshness,
    parse_descriptions_to_manifest,
    parse_glass_data_results,
)


# ─── FT-1: Worker Crash Handling ─────────────────────────────────────────────


class TestFT1_WorkerCrashHandling:
    """GPO must stop immediately on worker failure — no email, no Excel update."""

    def test_pipeline_aborts_on_worker_failure(self, tmp_path, monkeypatch):
        """Simulate GlassDataParser.py returning exit code 1.
        Pipeline must not reach Phase 5 (persist) or Phase 6 (notify)."""

        # Create a fake worker script that exits with error
        bad_worker = tmp_path / "GlassDataParser.py"
        bad_worker.write_text("import sys; sys.exit(1)\n")

        # Patch configuration to use temp paths
        monkeypatch.setattr("GlassOrchestrator.WORKER_SCRIPT", bad_worker)
        monkeypatch.setattr("GlassOrchestrator.DATA_DIR", tmp_path)
        monkeypatch.setattr("GlassOrchestrator.CSV_PATH", tmp_path / "GlassDataParser.csv")

        # Worker step should raise CalledProcessError
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            parse_glass_data_results(["59340120", "59340121"])
        assert exc_info.value.returncode == 1

    def test_full_pipeline_stops_after_worker_crash(self, tmp_path, monkeypatch):
        """Run full pipeline with a crashing worker — Phase 5/6 must NOT execute."""
        bad_worker = tmp_path / "GlassDataParser.py"
        bad_worker.write_text("import sys; sys.exit(1)\n")

        monkeypatch.setattr("GlassOrchestrator.WORKER_SCRIPT", bad_worker)
        monkeypatch.setattr("GlassOrchestrator.DATA_DIR", tmp_path)
        monkeypatch.setattr("GlassOrchestrator.CSV_PATH", tmp_path / "GlassDataParser.csv")

        # Mock input acquisition to return test data (bypass Gmail)
        mock_descriptions = [("0305APO", "59340120WS"), ("0305APO", "59340121WSr")]
        mock_date = datetime(2026, 3, 5)
        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: (mock_descriptions, mock_date),
        )

        # Track whether persistence / notify are called
        persist_called = False
        notify_called = False
        original_persist = __import__("GlassOrchestrator").persist_new_rows
        original_notify = __import__("GlassOrchestrator").notify_order_items

        def spy_persist(df):
            nonlocal persist_called
            persist_called = True
            return original_persist(df)

        def spy_notify(df):
            nonlocal notify_called
            notify_called = True
            return original_notify(df)

        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", spy_persist)
        monkeypatch.setattr("GlassOrchestrator.notify_order_items", spy_notify)

        # Run the pipeline — should not crash, but should abort after worker step
        run_pipeline()

        assert not persist_called, "Persistence should NOT have been called after worker crash"
        assert not notify_called, "Notify should NOT have been called after worker crash"

    def test_missing_worker_script_raises_file_not_found(self, tmp_path, monkeypatch):
        """WORKER_SCRIPT path does not exist → FileNotFoundError raised before subprocess call."""
        missing_script = tmp_path / "missing" / "GlassDataParser.py"
        csv_path = tmp_path / "GlassDataParser.csv"

        monkeypatch.setattr("GlassOrchestrator.WORKER_SCRIPT", missing_script)
        monkeypatch.setattr("GlassOrchestrator.DATA_DIR", tmp_path)
        monkeypatch.setattr("GlassOrchestrator.CSV_PATH", csv_path)

        with pytest.raises(FileNotFoundError) as exc_info:
            parse_glass_data_results(["59340120", "59340121"])

        assert csv_path.exists()
        assert csv_path.read_text(encoding="utf-8").splitlines() == [
            "MVA",
            "59340120",
            "59340121",
        ]
        assert "correct the WORKER_SCRIPT path" in str(exc_info.value)

    def test_full_pipeline_aborts_when_worker_script_missing(self, tmp_path, monkeypatch):
        """Full pipeline: missing WORKER_SCRIPT → abort before persist/notify."""
        missing_script = tmp_path / "missing" / "GlassDataParser.py"

        monkeypatch.setattr("GlassOrchestrator.WORKER_SCRIPT", missing_script)
        monkeypatch.setattr("GlassOrchestrator.DATA_DIR", tmp_path)
        monkeypatch.setattr("GlassOrchestrator.CSV_PATH", tmp_path / "GlassDataParser.csv")
        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([("0305APO", "59340120WS")], datetime(2026, 3, 5)),
        )

        persist_called = False
        notify_called = False

        def spy_persist(df):
            nonlocal persist_called
            persist_called = True
            return df

        def spy_notify(df):
            nonlocal notify_called
            notify_called = True

        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", spy_persist)
        monkeypatch.setattr("GlassOrchestrator.notify_order_items", spy_notify)

        run_pipeline()

        assert not persist_called
        assert not notify_called


# ─── FT-2: Stale Results Protection ──────────────────────────────────────────


class TestFT2_StaleResultsProtection:
    """GPO must detect stale GlassResults.txt and throw a fatal error."""

    def test_fresh_file_passes(self, tmp_path):
        """A file modified just now should pass validation."""
        results = tmp_path / "GlassResults.txt"
        results.write_text("MVA,VIN,Desc\n59340120,ABC123,Windshield\n")
        # File was just created → fresh
        validate_results_freshness(results, max_age_seconds=300)  # should not raise

    def test_stale_file_raises(self, tmp_path):
        """A file last modified >24h ago must raise RuntimeError."""
        results = tmp_path / "GlassResults.txt"
        results.write_text("MVA,VIN,Desc\n59340120,ABC123,Windshield\n")

        # Backdate the file's mtime by 25 hours
        old_time = time.time() - (25 * 3600)
        os.utime(str(results), (old_time, old_time))

        with pytest.raises(RuntimeError, match="Stale results file"):
            validate_results_freshness(results, max_age_seconds=300)

    def test_missing_file_raises(self, tmp_path):
        """No results file at all → RuntimeError."""
        with pytest.raises(RuntimeError, match="Results file not found"):
            validate_results_freshness(tmp_path / "nonexistent.txt")

    def test_pipeline_aborts_on_stale_results(self, tmp_path, monkeypatch):
        """Full pipeline: stale results -> abort before merge step."""
        results_file = tmp_path / "GlassResults.txt"
        results_file.write_text("MVA,VIN,Desc\n59340120,ABC123,Windshield\n")

        # Backdate mtime
        old_time = time.time() - (25 * 3600)
        os.utime(str(results_file), (old_time, old_time))

        # Create a worker that "succeeds" but leaves a stale file
        ok_worker = tmp_path / "GlassDataParser.py"
        ok_worker.write_text("pass\n")  # exits 0 but doesn't update results

        monkeypatch.setattr("GlassOrchestrator.WORKER_SCRIPT", ok_worker)
        monkeypatch.setattr("GlassOrchestrator.RESULTS_PATH", results_file)
        monkeypatch.setattr("GlassOrchestrator.DATA_DIR", tmp_path)
        monkeypatch.setattr("GlassOrchestrator.CSV_PATH", tmp_path / "GlassDataParser.csv")

        mock_descriptions = [("0305APO", "59340120WS")]
        mock_date = datetime(2026, 3, 5)
        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: (mock_descriptions, mock_date),
        )

        merge_called = False
        original_merge = __import__("GlassOrchestrator").merge_manifest_with_results

        def spy_merge(manifest):
            nonlocal merge_called
            merge_called = True
            return original_merge(manifest)

        monkeypatch.setattr("GlassOrchestrator.merge_manifest_with_results", spy_merge)

        run_pipeline()

        assert not merge_called, "Merge should NOT execute when results are stale"


# ─── FT-3: Notification/Persistence Consistency ─────────────────────────────


class TestFT3_NotificationConsistency:
    """Email payload should be based on rows actually written to the sheet."""

    def test_pipeline_notifies_only_persisted_rows(self, monkeypatch):
        email_date = datetime(2026, 3, 9)
        monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", True)

        df_merged = pd.DataFrame([
            {
                "Inventory Date": "03/09/2026",
                "Original Date": "03/09/2026",
                "MVA": "01712003",
                "VIN": "N/A",
                "Make": "N/A",
                "Location": "APO",
                "Action": "Replacement",
                "Area": "Windshield",
                "Claim#": "Listed",
                "WorkItem": "verified",
            },
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
        ])

        # Simulate idempotency skipping the N/A row; only one row is newly persisted.
        df_new_rows = df_merged[df_merged["MVA"] == "59654641"].copy()

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([("0309APO", "59654641WS"), ("0309APO", "01712003WS")], email_date),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda descriptions, dt: ({"59654641": {}, "01712003": {}}, ["59654641", "01712003"]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *args, **kwargs: None)
        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", lambda *args, **kwargs: None)
        monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", lambda *args, **kwargs: None)
        monkeypatch.setattr("GlassOrchestrator.merge_manifest_with_results", lambda manifest: df_merged)
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", lambda df: df_new_rows)

        notified_mvas: list[str] = []

        def spy_notify(df):
            nonlocal notified_mvas
            notified_mvas = df["MVA"].tolist()

        monkeypatch.setattr("GlassOrchestrator.notify_order_items", spy_notify)

        run_pipeline()

        assert notified_mvas == ["59654641"]
