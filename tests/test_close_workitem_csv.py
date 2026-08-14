"""Tests for _load_csv comment-line skipping in WorkItems/close_workitem.py."""
import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _write_csv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.csv"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadCsvCommentSkipping:

    def test_comment_lines_skipped(self, tmp_path):
        from WorkItems.close_workitem import _load_csv
        csv_path = _write_csv(
            tmp_path,
            "# header comment\nmva,Type\n# row comment\n11111,Glass\n",
        )
        rows = _load_csv(str(csv_path))
        assert len(rows) == 1
        assert rows[0]["mva"] == "11111"

    def test_blank_mva_skipped(self, tmp_path):
        from WorkItems.close_workitem import _load_csv
        csv_path = _write_csv(tmp_path, "mva,Type\n,Glass\n22222,PM\n")
        rows = _load_csv(str(csv_path))
        assert len(rows) == 1
        assert rows[0]["mva"] == "22222"

    def test_complaint_type_patterns_imported_from_steps(self):
        from WorkItems.close_workitem import COMPLAINT_TYPE_PATTERNS
        from playwright_prototype.steps import COMPLAINT_TYPE_PATTERNS as steps_patterns
        assert COMPLAINT_TYPE_PATTERNS is steps_patterns

    def test_default_build_uses_reviewed_csv_not_sheet(self, monkeypatch, tmp_path):
        from WorkItems import close_workitem

        csv_path = _write_csv(tmp_path, "mva,Type\n12345678,Glass\n")
        sheet_loader = AsyncMock()
        monkeypatch.setattr(close_workitem, "_load_sheet_rows", sheet_loader)
        args = type("Args", (), {"mvas": "", "csv_path": str(csv_path), "max_rows": None})()

        targets = close_workitem._build_targets(args)

        assert targets == [{"mva": "012345678", "complaint_type": "Glass"}]
        sheet_loader.assert_not_called()


class TestCloseMissingComplaint:

    def test_missing_glass_complaint_returns_not_found(self, monkeypatch):
        from WorkItems import close_workitem

        close_work_item = AsyncMock(
            side_effect=LookupError("Open Work Items row not found: GLASS-GLASS")
        )
        monkeypatch.setattr(close_workitem, "close_open_work_item", close_work_item)

        result = asyncio.run(
            close_workitem._playwright_close_work_item(
                AsyncMock(), "062021481", complaint_type="Glass"
            )
        )

        assert result == (close_workitem.RESULT_NOT_FOUND, "")
        close_work_item.assert_awaited_once()


class TestActiveCloseRunnerDisconnect:

    def test_browser_disconnect_returns_partial_results_without_cleanup_crash(self, monkeypatch):
        from WorkItems import close_workitem

        page = AsyncMock()
        page.url = "https://avisbudget.palantirfoundry.com/workspace/vehicle"
        context = AsyncMock()
        context.close = AsyncMock(side_effect=RuntimeError("connection closed"))

        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        playwright_context = AsyncMock()
        playwright_context.__aenter__ = AsyncMock(return_value=playwright)
        playwright_context.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(close_workitem, "async_playwright", lambda: playwright_context)
        monkeypatch.setattr(close_workitem, "_is_edge_running", lambda: False)
        monkeypatch.setattr(close_workitem, "resolve_headless", lambda: False)
        monkeypatch.setattr(close_workitem, "resolve_edge_user_data_dir", lambda: "profile")
        monkeypatch.setattr(close_workitem, "resolve_edge_profile_directory", lambda: "Default")
        monkeypatch.setattr(close_workitem, "resolve_step_delay", lambda: 0)
        monkeypatch.setattr(
            close_workitem,
            "ensure_profile_context",
            AsyncMock(return_value=(context, page)),
        )
        monkeypatch.setattr(close_workitem, "pw_navigate_to_mva", AsyncMock(return_value=page))
        monkeypatch.setattr(
            close_workitem,
            "_playwright_close_work_item",
            AsyncMock(return_value=(close_workitem.RESULT_NOT_FOUND, "")),
        )
        monkeypatch.setattr(close_workitem, "_capture_playwright_screenshot", AsyncMock())
        monkeypatch.setattr(
            close_workitem,
            "_ensure_live_page",
            AsyncMock(side_effect=[page, page, RuntimeError("browser disconnected")]),
        )

        args = argparse.Namespace(timeout_seconds=30, debug_hold_seconds=0)
        targets = [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
            {"mva": "033333333", "complaint_type": "Glass"},
        ]

        results = asyncio.run(close_workitem._run_playwright_close_async(args, targets))

        assert results == [
            {"mva": "011111111", "result": close_workitem.RESULT_NOT_FOUND, "detail": ""},
            {
                "mva": "022222222",
                "result": close_workitem.RESULT_ERROR,
                "detail": "browser unavailable",
            },
        ]


class TestCloseProcessingCheckpoint:

    def test_sheet_targets_skip_previously_processed_mva(self, monkeypatch, tmp_path):
        from WorkItems import close_workitem

        state_path = tmp_path / "close_workitem_processed.json"
        state_path.write_text(
            json.dumps({
                "date": date.today().isoformat(),
                "processed": [
                    {"mva": "011111111", "complaint_type": "Glass"},
                ],
            }),
            encoding="utf-8",
        )
        today = date.today().strftime("%m/%d/%Y")
        monkeypatch.setattr(close_workitem, "PROCESSED_STATE_PATH", state_path)
        monkeypatch.setattr(
            close_workitem,
            "_load_sheet_rows",
            lambda: [
                {"MVA": "11111111", "Damage Type": "Replacement", "Inventory Date": today, "Repair Status": "Completed"},
                {"MVA": "22222222", "Damage Type": "Replacement", "Inventory Date": today, "Repair Status": "Completed"},
            ],
        )

        targets = close_workitem._build_targets_from_sheet(max_rows=1)

        assert targets == [{"mva": "022222222", "complaint_type": "Glass"}]

    def test_records_closed_and_not_found_but_not_failures(self, monkeypatch, tmp_path):
        from WorkItems import close_workitem

        state_path = tmp_path / "close_workitem_processed.json"
        monkeypatch.setattr(close_workitem, "PROCESSED_STATE_PATH", state_path)
        targets = [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
            {"mva": "033333333", "complaint_type": "Glass"},
        ]
        results = [
            {"mva": "011111111", "result": close_workitem.RESULT_CLOSED},
            {"mva": "022222222", "result": close_workitem.RESULT_NOT_FOUND},
            {"mva": "033333333", "result": close_workitem.RESULT_ERROR},
        ]

        close_workitem._record_processed_targets(targets, results)

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["processed"] == [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
        ]

    def test_retires_closed_and_not_found_rows_from_csv(self, tmp_path):
        from WorkItems import close_workitem

        csv_path = tmp_path / "close_workitem.csv"
        history_path = tmp_path / "close_workitem_history.csv"
        csv_path.write_text(
            "# reviewed queue\nmva,Type\n11111111,Glass\n22222222,Glass\n33333333,Glass\n",
            encoding="utf-8",
        )
        targets = [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
            {"mva": "033333333", "complaint_type": "Glass"},
        ]
        results = [
            {"mva": "011111111", "result": close_workitem.RESULT_CLOSED},
            {"mva": "022222222", "result": close_workitem.RESULT_NOT_FOUND},
            {"mva": "033333333", "result": close_workitem.RESULT_ERROR},
        ]

        retired = close_workitem._retire_handled_csv_targets(
            csv_path,
            targets,
            results,
            history_path,
        )

        assert retired == 2
        assert "33333333,Glass" in csv_path.read_text(encoding="utf-8")
        assert "11111111,Glass" not in csv_path.read_text(encoding="utf-8")
        history = history_path.read_text(encoding="utf-8")
        assert "011111111,Glass" in history
        assert "022222222,Glass" in history

    def test_sheet_targets_require_resolved_status_and_sort_oldest_first(self, monkeypatch, tmp_path):
        from WorkItems import close_workitem

        monkeypatch.setattr(
            close_workitem,
            "PROCESSED_STATE_PATH",
            tmp_path / "close_workitem_processed.json",
        )
        monkeypatch.setattr(
            close_workitem,
            "_load_sheet_rows",
            lambda: [
                {"MVA": "11111111", "Damage Type": "Replacement", "Inventory Date": "08/12/2026", "Repair Status": "Scheduled"},
                {"MVA": "22222222", "Damage Type": "Replacement", "Inventory Date": "08/10/2026", "Repair Status": "Completed"},
                {"MVA": "33333333", "Damage Type": "Replacement", "Inventory Date": "08/01/2026", "Repair Status": "Closed"},
                {"MVA": "44444444", "Damage Type": "Replacement", "Inventory Date": "08/05/2026", "Repair Status": "Resolved"},
            ],
        )

        targets = close_workitem._build_targets_from_sheet(max_rows=2)

        assert targets == [
            {"mva": "033333333", "complaint_type": "Glass"},
            {"mva": "044444444", "complaint_type": "Glass"},
        ]
