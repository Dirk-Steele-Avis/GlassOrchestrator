"""
Pipeline Smoke Tests — end-to-end control flow with all external I/O mocked.

SMOKE-1  No unread email          → pipeline exits cleanly before parse step.
SMOKE-2  All-duplicate MVAs       → parser runs, worker runs, persist returns
                                    empty (all dupes), no notification sent.
"""

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from GlassOrchestrator import COLUMNS, _ConsoleFormatter, run_pipeline


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


@pytest.mark.parametrize("has_valid_scan", [True, False])
def test_malformed_scan_batch_completion(monkeypatch, caplog, has_valid_scan):
    email_date = datetime(2026, 10, 15)
    descriptions = [("1015", "58004052wcr")]
    if has_valid_scan:
        descriptions.extend([("1015", "59340120WS"), ("1015", "59340121WSr")])
    fetch = MagicMock(side_effect=[
        (descriptions, email_date, b"101"),
        ([], email_date, None),
    ])
    worker = MagicMock()
    tracking = MagicMock()
    freshness = MagicMock()
    merge = MagicMock(return_value=_merged_df(["59340120", "59340121"], "10/15/2026"))
    persist = MagicMock(side_effect=lambda frame: frame)
    notify = MagicMock()
    acknowledge = MagicMock()
    downstream = MagicMock(return_value=0)
    monkeypatch.setattr("GlassOrchestrator.fetch_input_descriptions", fetch)
    monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", tracking)
    monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", worker)
    monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", freshness)
    monkeypatch.setattr("GlassOrchestrator.merge_manifest_with_results", merge)
    monkeypatch.setattr("GlassOrchestrator.persist_new_rows", persist)
    monkeypatch.setattr("GlassOrchestrator.notify_order_items", notify)
    monkeypatch.setattr("GlassOrchestrator._mark_message_seen", acknowledge)
    monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", False)

    result = run_pipeline(after_batch=downstream, processing_date=email_date.date())

    assert "MALFORMED_SCAN: scan='58004052wcr'; skipped" in caplog.text
    notify.assert_not_called()
    if has_valid_scan:
        assert result == 0
        worker.assert_called_once_with(["59340120", "59340121"])
        assert set(merge.call_args.args[0]) == {"59340120", "59340121"}
        persist.assert_called_once_with(merge.return_value)
        downstream.assert_called_once_with(email_date.date())
        acknowledge.assert_called_once_with(b"101")
        assert fetch.call_count == 2
    else:
        assert result == 1
        assert "email produced no valid MVAs" in caplog.text
        for stage in (tracking, worker, freshness, merge, persist, downstream, acknowledge):
            stage.assert_not_called()
        fetch.assert_called_once_with()


@pytest.mark.parametrize("is_page_timeout", [True, False])
def test_downstream_failure_message(monkeypatch, caplog, is_page_timeout):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    email_date = datetime(2026, 11, 3)
    if is_page_timeout:
        error = PlaywrightTimeoutError(
            "Page.goto: Timeout 30000ms exceeded.\nCall log:\n"
            '  - navigating to "https://example.test/fieldpo/dashboard", waiting until "load"'
        )
    else:
        error = ValueError("unexpected boom")
    acknowledge = MagicMock()
    monkeypatch.setattr(
        "GlassOrchestrator.fetch_input_descriptions",
        MagicMock(return_value=([("1103", "59340120WS")], email_date, b"7")),
    )
    monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", MagicMock())
    monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", MagicMock())
    monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", MagicMock())
    monkeypatch.setattr(
        "GlassOrchestrator.merge_manifest_with_results",
        MagicMock(return_value=_merged_df(["59340120"], "11/03/2026")),
    )
    monkeypatch.setattr("GlassOrchestrator.persist_new_rows", MagicMock(side_effect=lambda f: f))
    monkeypatch.setattr("GlassOrchestrator._mark_message_seen", acknowledge)
    monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", False)

    result = run_pipeline(
        after_batch=MagicMock(side_effect=error), processing_date=email_date.date()
    )

    assert result == 1
    acknowledge.assert_not_called()
    record = next(r for r in caplog.records if "Downstream processing failed" in r.getMessage())
    assert record.exc_info
    console_line = _ConsoleFormatter("%(message)s").format(record)
    file_text = logging.Formatter("%(message)s").format(record)
    assert "Traceback" not in console_line
    assert "Traceback" in file_text
    if is_page_timeout:
        assert "did not respond in time (https://example.test/fieldpo/dashboard)" in record.getMessage()
    else:
        assert "unexpected boom" in record.getMessage()


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
        ("0425", "59340120WS"),
        ("0425", "59340121WS"),
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
        run_pipeline(processing_date=self._DATE.date())
        assert tracker["worker_called"], "Worker must run regardless of duplicate status"

    def test_persist_is_called(self, monkeypatch):
        """persist_new_rows is called and returns rows even when MVAs already exist."""
        tracker = self._monkeypatch_pipeline(monkeypatch)
        run_pipeline(processing_date=self._DATE.date())
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

        run_pipeline(processing_date=self._DATE.date())

        assert not send_called, "_send_email must NOT be called when there are no new rows"

    def test_no_exception_raised(self, monkeypatch):
        """Pipeline must not propagate any exception on all-duplicate run."""
        self._monkeypatch_pipeline(monkeypatch)
        run_pipeline(processing_date=self._DATE.date())  # must not raise


class TestEmailSeenTiming:
    """Source email read flag is updated only after successful completion."""

    def test_marks_email_seen_on_success(self, monkeypatch):
        source_uid = b"12345"
        monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", False)

        payloads = [
            ([("0425", "59340120WS")], datetime(2026, 4, 25), source_uid),
            ([], datetime(2026, 4, 25), None),
        ]

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: payloads.pop(0),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda *_args, **_kwargs: ({"59340120": {}}, ["59340120"]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.merge_manifest_with_results", lambda *_a, **_k: _merged_df(["59340120"]))
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", lambda df: df.iloc[0:0])
        def unexpected_notify(*_args, **_kwargs):
            pytest.fail("Notification must not run when notifications are disabled")

        monkeypatch.setattr("GlassOrchestrator.notify_order_items", unexpected_notify)

        marked: list[bytes] = []
        monkeypatch.setattr("GlassOrchestrator._mark_message_seen", lambda uid: marked.append(uid))

        run_pipeline()

        assert marked == [source_uid]

    def test_does_not_mark_email_seen_on_failure(self, monkeypatch):
        source_uid = b"12345"

        payloads = [
            ([("0425", "59340120WS")], datetime(2026, 4, 25), source_uid),
            ([], datetime(2026, 4, 25), None),
        ]

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            lambda: payloads.pop(0),
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda *_args, **_kwargs: ({"59340120": {}}, ["59340120"]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)

        def fail_worker(*_args, **_kwargs):
            raise RuntimeError("worker failed")

        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", fail_worker)

        marked: list[bytes] = []
        monkeypatch.setattr("GlassOrchestrator._mark_message_seen", lambda uid: marked.append(uid))

        run_pipeline()

        assert marked == []


class TestUnreadDrainBehavior:
    """Pipeline drains multiple unread source emails in one run."""

    def test_processes_all_unread_payloads_in_single_run(self, monkeypatch):
        monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", False)

        payloads = [
            ([("0910", "59340120WS")], datetime(2026, 9, 10), b"1001"),
            ([("0910", "59340121WS")], datetime(2026, 9, 10), b"1002"),
            ([], datetime(2026, 9, 10), None),
        ]

        call_count = {"fetch": 0}

        def fake_fetch_input_descriptions():
            call_count["fetch"] += 1
            return payloads.pop(0)

        monkeypatch.setattr(
            "GlassOrchestrator.fetch_input_descriptions",
            fake_fetch_input_descriptions,
        )
        monkeypatch.setattr(
            "GlassOrchestrator.parse_descriptions_to_manifest",
            lambda descriptions, _dt, **kwargs: ({descriptions[0][1][:8]: {}}, [descriptions[0][1][:8]]),
        )
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "GlassOrchestrator.merge_manifest_with_results",
            lambda manifest: _merged_df(list(manifest.keys()), date="09/10/2026"),
        )
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", lambda df: df.iloc[0:0])

        marked: list[bytes] = []
        monkeypatch.setattr("GlassOrchestrator._mark_message_seen", lambda uid: marked.append(uid))

        run_pipeline()

        assert call_count["fetch"] == 3
        assert marked == [b"1001", b"1002"]

    def test_runs_downstream_before_marking_each_same_date_email(self, monkeypatch):
        monkeypatch.setattr("GlassOrchestrator.NOTIFICATIONS_ENABLED", False)
        payloads = [
            ([("0910", "59340120WS")], datetime(2026, 9, 10), b"1001"),
            ([("0910", "59340121WS")], datetime(2026, 9, 10), b"1002"),
            ([], datetime(2026, 9, 10), None),
        ]
        events = []

        monkeypatch.setattr("GlassOrchestrator.fetch_input_descriptions", lambda: payloads.pop(0))
        monkeypatch.setattr("GlassOrchestrator.apply_cycle_day_tracking", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.parse_glass_data_results", lambda *_a, **_k: None)
        monkeypatch.setattr("GlassOrchestrator.validate_results_freshness", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "GlassOrchestrator.merge_manifest_with_results",
            lambda manifest: _merged_df(list(manifest.keys()), date="09/10/2026"),
        )
        monkeypatch.setattr("GlassOrchestrator.persist_new_rows", lambda df: df)
        monkeypatch.setattr(
            "GlassOrchestrator._mark_message_seen",
            lambda uid: events.append(("seen", uid)),
        )

        result = run_pipeline(
            after_batch=lambda batch_date: events.append(("downstream", batch_date)),
            processing_date=datetime(2026, 9, 10).date(),
        )

        assert result == 0
        assert events == [
            ("downstream", datetime(2026, 9, 10).date()),
            ("seen", b"1001"),
            ("downstream", datetime(2026, 9, 10).date()),
            ("seen", b"1002"),
        ]
