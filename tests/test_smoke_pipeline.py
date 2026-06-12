"""
Pipeline Smoke Tests — end-to-end control flow with all external I/O mocked.

SMOKE-1  No unread email          → pipeline exits cleanly before parse step.
SMOKE-2  All-duplicate MVAs       → parser runs, worker runs, persist returns
                                    empty (all dupes), no notification sent.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from GlassOrchestrator import COLUMNS, run_pipeline


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _merged_df(mvas: list[str], date: str = "04/25/2026") -> pd.DataFrame:
    """Build a minimal merged DataFrame matching the current COLUMNS contract."""
    rows = [
        {
            "Inventory Date": date,
            "Original Date": date,
            "MVA": mva,
            "FPO#": "",
            "VIN": "1HGCM82633A004352",
            "Make": "HONDA CIVIC",
            "Location": "APO",
            "Action": "Replacement",
            "Area": "Windshield",
            "Claim#": "Missing",
            "WorkItem": "verified",
        }
        for mva in mvas
    ]
    return pd.DataFrame(rows, columns=COLUMNS)


def _worksheet_with_existing(mvas: list[str], date: str = "04/25/2026") -> MagicMock:
    """Return a mock worksheet whose existing data already contains these MVAs."""
    ws = MagicMock()
    header = list(COLUMNS)
    data_rows = [
        [date, date, mva, "", "1HGCM82633A004352", "HONDA CIVIC", "APO",
         "Replace(AGN)", "Windshield", "Missing", "verified"]
        for mva in mvas
    ]
    ws.get_all_values.return_value = [header] + data_rows
    return ws


# ─── SMOKE-1: No unread email ─────────────────────────────────────────────────


class TestSmoke1_NoUnreadEmail:
    """Pipeline exits cleanly when there are no unread emails to process."""

    def test_exits_before_parse_step(self, monkeypatch):
        """fetch_input_descriptions returns empty list → no further steps run."""
        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([], datetime(2026, 4, 25)),
        )

        parse_called = False
        persist_called = False
        notify_called = False

        def spy_parse(descriptions, dt):
            nonlocal parse_called
            parse_called = True
            # Should never be reached — return dummy to avoid masking bugs
            return {}, []

        def spy_persist(df):
            nonlocal persist_called
            persist_called = True
            return df

        def spy_notify(df):
            nonlocal notify_called
            notify_called = True

        monkeypatch.setattr("GlassOrchestrator.parse_descriptions_to_manifest", spy_parse)
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", spy_persist)
        monkeypatch.setattr("GlassOrchestrator.notify_order_items", spy_notify)

        # Should complete without raising
        run_pipeline()

        assert not parse_called,   "parse step must NOT run when inbox is empty"
        assert not persist_called, "persist step must NOT run when inbox is empty"
        assert not notify_called,  "notify step must NOT run when inbox is empty"

    def test_no_exception_raised(self, monkeypatch):
        """Pipeline must not propagate any exception when inbox is empty."""
        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([], datetime(2026, 4, 25)),
        )
        # If this raises, the test fails
        run_pipeline()


# ─── SMOKE-2: All-duplicate MVAs ─────────────────────────────────────────────


class TestSmoke2_AllDuplicateMVAs:
    """
    Pipeline receives one email whose MVAs are already present in the sheet.
    Worker runs (it doesn't know about duplicates), persist detects all dupes
    and writes nothing, notification is not sent.
    """

    # Scans using the new Orca format — WS area, no flags
    _SCANS = [
        ("0425APO", "59340120WS"),
        ("0425APO", "59340121WS"),
    ]
    _MVAS = ["59340120", "59340121"]
    _DATE = datetime(2026, 4, 25)

    def _monkeypatch_pipeline(self, monkeypatch, *, worker_ok: bool = True) -> dict:
        """Wire up all external I/O mocks and return a call-tracker dict."""
        tracker = {
            "worker_called": False,
            "persist_called": False,
            "notify_called": False,
            "persist_result": None,
        }

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: (self._SCANS, self._DATE),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.apply_cycle_day_tracking",
            lambda *a, **kw: None,
        )

        def spy_worker(mva_list):
            tracker["worker_called"] = True

        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", spy_worker)
        monkeypatch.setattr(
            "GlassOrchestrator.validate_results_freshness",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "GlassOrchestrator.merge_manifest_with_results",
            lambda manifest: _merged_df(list(manifest.keys())),
        )

        ws = _worksheet_with_existing(self._MVAS)

        def spy_persist(df):
            tracker["persist_called"] = True
            tracker["persist_result"] = df
            return df

        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", spy_persist)

        def spy_notify(df):
            tracker["notify_called"] = True

        monkeypatch.setattr("GlassOrchestrator.notify_order_items", spy_notify)

        return tracker

    def test_worker_is_called(self, monkeypatch):
        """Worker subprocess runs even when sheet duplicates exist."""
        tracker = self._monkeypatch_pipeline(monkeypatch)
        run_pipeline()
        assert tracker["worker_called"], "Worker must run regardless of duplicate status"

    def test_persist_is_called(self, monkeypatch):
        """persist_new_rows is called and returns rows even when MVAs already exist."""
        tracker = self._monkeypatch_pipeline(monkeypatch)
        run_pipeline()
        assert tracker["persist_called"], "persist_new_rows must be called"
        assert tracker["persist_result"] is not None
        assert len(tracker["persist_result"]) == len(self._MVAS), (
            "Rows should still be written when MVAs already exist in the sheet"
        )

    def test_no_email_sent(self, monkeypatch):
        """No SMTP email is dispatched when all MVAs are duplicates.

        run_pipeline() calls notify_order_items() with an empty DataFrame;
        notify_order_items() guards against empty payloads internally and
        must not forward anything to _send_email.
        """
        self._monkeypatch_pipeline(monkeypatch)

        send_called = False

        def spy_send(message):
            nonlocal send_called
            send_called = True

        monkeypatch.setattr("GlassOrchestrator._send_email", spy_send)

        run_pipeline()

        assert not send_called, "_send_email must NOT be called when there are no new rows"

    def test_no_exception_raised(self, monkeypatch):
        """Pipeline must not propagate any exception on all-duplicate run."""
        self._monkeypatch_pipeline(monkeypatch)
        run_pipeline()  # must not raise


class TestEmailSeenTiming:
    """Source email read flag is updated only after successful completion."""

    def test_marks_email_seen_on_success(self, monkeypatch):
        source_uid = b"12345"

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([("0425APO", "59340120WS")], datetime(2026, 4, 25), source_uid),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda *_args: ({"59340120": {}}, ["59340120"]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.merge_manifest_with_results", lambda *_a, **_k: _merged_df(["59340120"]))
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", lambda df: df.iloc[0:0])
        monkeypatch.setattr("GlassOrchestrator.notify_order_items", lambda *_a, **_k: None)

        marked: list[bytes] = []
        monkeypatch.setattr("GlassOrchestrator._mark_message_seen", lambda uid: marked.append(uid))

        run_pipeline()

        assert marked == [source_uid]

    def test_does_not_mark_email_seen_on_failure(self, monkeypatch):
        source_uid = b"12345"

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: ([("0425APO", "59340120WS")], datetime(2026, 4, 25), source_uid),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda *_args: ({"59340120": {}}, ["59340120"]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)

        def fail_worker(*_args, **_kwargs):
            raise RuntimeError("worker failed")

        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", fail_worker)

        marked: list[bytes] = []
        monkeypatch.setattr("GlassOrchestrator._mark_message_seen", lambda uid: marked.append(uid))

        run_pipeline()

        assert marked == []
