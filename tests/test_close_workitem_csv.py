"""Tests for _load_csv comment-line skipping in WorkItems/close_workitem.py."""
import argparse
import asyncio
from datetime import date
import io
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_not_found_summary_displays_zero_work_item_count(monkeypatch):
    from WorkItems import close_workitem

    info = MagicMock()
    monkeypatch.setattr(close_workitem.log, "info", info)

    not_found, failed = close_workitem._log_summary([
        {"mva": "022222222", "result": close_workitem.RESULT_NOT_FOUND, "detail": ""},
    ])

    assert (not_found, failed) == (1, 0)
    assert any(
        call.args == ("[CLOSE]   - Work item count 0: %d", 1)
        for call in info.call_args_list
    )
    assert any(
        call.args[0] == "[CLOSE]   %s  %12s  [%s]%s"
        and call.args[2:4] == ("022222222", "work_item_count_0")
        for call in info.call_args_list
    )


def test_concise_console_summary_emits_readable_stage_and_mva_results(capsys):
    from WorkItems import close_workitem

    close_workitem._print_console_summary([
        {"mva": "022222222", "result": close_workitem.RESULT_NOT_FOUND, "detail": ""},
        {"mva": "033333333", "result": close_workitem.RESULT_CLOSED, "detail": "marked complete"},
    ])

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "[CLOSEOUT] Work items: 2 target(s); 1 closed, 1 not found, "
        "0 failed, 0 not attempted."
    )
    assert lines[1] == "[CLOSEOUT] MVA 022222222 | NOT FOUND | No open work item found."
    assert lines[2] == "[CLOSEOUT] MVA 033333333 | CLOSED | marked complete"


def test_write_results_json_preserves_complaint_type_and_outcome(tmp_path):
    from WorkItems import close_workitem

    path = tmp_path / "closeout-results.json"
    close_workitem._write_results_json(
        str(path),
        [{
            "mva": "058889320",
            "complaint_type": "Glass",
            "result": close_workitem.RESULT_CLOSED,
            "detail": "GLASS-GLASS",
        }],
    )

    assert json.loads(path.read_text(encoding="utf-8")) == [
        {
            "mva": "058889320",
            "complaint_type": "Glass",
            "status": "closed",
            "detail": "GLASS-GLASS",
        },
    ]


def test_concise_console_keeps_info_in_file_handler(tmp_path, monkeypatch):
    from WorkItems import close_workitem

    root_logger = logging.getLogger()
    console_handler = logging.StreamHandler(io.StringIO())
    file_handler = logging.FileHandler(tmp_path / "close.log", encoding="utf-8")
    monkeypatch.setattr(root_logger, "handlers", [console_handler, file_handler])
    try:
        close_workitem._configure_concise_console()
        assert console_handler.level == logging.WARNING
        assert file_handler.level == logging.NOTSET
    finally:
        console_handler.close()
        file_handler.close()


def _write_csv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.csv"
    p.write_text(content, encoding="utf-8")
    return p


def test_invoice_scope_selects_exact_mva_from_existing_queue(tmp_path):
    from WorkItems import close_workitem

    path = _write_csv(
        tmp_path,
        "mva,Type\n11111111,Glass\n59235573,Glass\n22222222,Glass\n",
    )
    args = argparse.Namespace(
        mvas="", only_mvas="059235573", csv_path=str(path), max_rows=None,
    )
    assert close_workitem._build_targets(args) == [
        {"mva": "059235573", "complaint_type": "Glass"},
    ]


def test_invoice_scope_missing_from_queue_is_visible_failure(tmp_path):
    from WorkItems import close_workitem

    path = _write_csv(tmp_path, "mva,Type\n11111111,Glass\n")
    args = argparse.Namespace(
        mvas="", only_mvas="059235573", csv_path=str(path), max_rows=None,
    )
    with pytest.raises(ValueError, match="Selected work-item targets are missing"):
        close_workitem._build_targets(args)


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

    def test_successful_close_reuses_exact_vehicle_keyword_search(self, monkeypatch):
        from WorkItems import close_workitem

        process_command = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["tasklist"], returncode=0, stdout="msedge.exe 1234", stderr="",
            ),
        )
        monkeypatch.setattr(subprocess, "run", process_command)
        page = MagicMock()
        page.url = "https://avisbudget.palantirfoundry.com/workspace/vehicle"
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        keyword_search = MagicMock()
        keyword_search.count = AsyncMock(return_value=1)
        keyword_search.first.wait_for = AsyncMock()
        page.locator.return_value = keyword_search
        context = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        playwright_context = AsyncMock()
        playwright_context.__aenter__ = AsyncMock(return_value=playwright)
        playwright_context.__aexit__ = AsyncMock(return_value=None)
        navigate = AsyncMock(return_value=page)

        monkeypatch.setattr(close_workitem, "async_playwright", lambda: playwright_context)
        monkeypatch.setattr(close_workitem, "resolve_headless", lambda: False)
        monkeypatch.setattr(close_workitem, "resolve_edge_user_data_dir", lambda: "profile")
        monkeypatch.setattr(close_workitem, "resolve_edge_profile_directory", lambda: "Default")
        monkeypatch.setattr(close_workitem, "resolve_step_delay", lambda: 0)
        monkeypatch.setattr(
            close_workitem,
            "ensure_profile_context",
            AsyncMock(return_value=(context, page)),
        )
        monkeypatch.setattr(close_workitem, "_ensure_live_page", AsyncMock(return_value=page))
        monkeypatch.setattr(close_workitem, "pw_navigate_to_mva", navigate)
        monkeypatch.setattr(
            close_workitem,
            "_playwright_close_work_item",
            AsyncMock(return_value=(close_workitem.RESULT_CLOSED, "GLASS-GLASS")),
        )
        monkeypatch.setattr(close_workitem, "_capture_playwright_screenshot", AsyncMock())

        args = argparse.Namespace(timeout_seconds=30, debug_hold_seconds=0)
        targets = [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
        ]

        results = asyncio.run(close_workitem._run_playwright_close_async(args, targets))

        assert [result["result"] for result in results] == [
            close_workitem.RESULT_CLOSED,
            close_workitem.RESULT_CLOSED,
        ]
        page.goto.assert_awaited_once_with(close_workitem.LOGIN_URL, wait_until="domcontentloaded")
        assert navigate.await_count == 2
        process_command.assert_not_called()
        playwright.chromium.launch_persistent_context.assert_awaited_once_with(
            user_data_dir="profile",
            channel="msedge",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--profile-directory=Default",
                "--start-maximized",
            ],
            no_viewport=True,
        )

    @pytest.mark.parametrize(
        "reason",
        ["ProcessSingleton: profile is already in use", "Executable doesn't exist"],
    )
    def test_browser_launch_failure_reports_exact_profile_without_retry_or_process_kill(
        self, monkeypatch, caplog, reason,
    ):
        from WorkItems import close_workitem

        launch = AsyncMock(side_effect=close_workitem.PlaywrightError(reason))
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = launch
        playwright_context = AsyncMock()
        playwright_context.__aenter__.return_value = playwright
        monkeypatch.setattr(close_workitem, "async_playwright", lambda: playwright_context)
        monkeypatch.setattr(close_workitem, "resolve_headless", lambda: False)
        monkeypatch.setattr(close_workitem, "resolve_edge_user_data_dir", lambda: "configured-profile")
        monkeypatch.setattr(close_workitem, "resolve_edge_profile_directory", lambda: "Default")
        monkeypatch.setattr(close_workitem, "resolve_step_delay", lambda: 0)
        process_command = MagicMock()
        monkeypatch.setattr(subprocess, "run", process_command)
        ensure_session = AsyncMock()
        close_item = AsyncMock()
        monkeypatch.setattr(close_workitem, "ensure_profile_context", ensure_session)
        monkeypatch.setattr(close_workitem, "_playwright_close_work_item", close_item)

        with pytest.raises(RuntimeError, match="Edge launch failed") as error:
            asyncio.run(close_workitem._run_playwright_close_async(
                argparse.Namespace(timeout_seconds=30, debug_hold_seconds=0),
                [{"mva": "059235573", "complaint_type": "Glass"}],
            ))

        assert "configured-profile" in str(error.value)
        assert "Default" in str(error.value)
        assert reason in str(error.value)
        assert reason in caplog.text
        launch.assert_awaited_once()
        ensure_session.assert_not_awaited()
        close_item.assert_not_awaited()
        process_command.assert_not_called()

    def test_navigation_failure_stops_remaining_mvas(self, monkeypatch):
        from WorkItems import close_workitem

        page = AsyncMock()
        page.url = "https://compass-home.avisbudget.palantirfoundry.com/"
        context = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        playwright_context = AsyncMock()
        playwright_context.__aenter__ = AsyncMock(return_value=playwright)
        playwright_context.__aexit__ = AsyncMock(return_value=None)
        navigate = AsyncMock(side_effect=RuntimeError("post-search contract changed"))

        monkeypatch.setattr(close_workitem, "async_playwright", lambda: playwright_context)
        monkeypatch.setattr(close_workitem, "resolve_headless", lambda: False)
        monkeypatch.setattr(close_workitem, "resolve_edge_user_data_dir", lambda: "profile")
        monkeypatch.setattr(close_workitem, "resolve_edge_profile_directory", lambda: "Default")
        monkeypatch.setattr(close_workitem, "resolve_step_delay", lambda: 0)
        monkeypatch.setattr(
            close_workitem,
            "ensure_profile_context",
            AsyncMock(return_value=(context, page)),
        )
        monkeypatch.setattr(close_workitem, "_ensure_live_page", AsyncMock(return_value=page))
        monkeypatch.setattr(close_workitem, "pw_navigate_to_mva", navigate)
        monkeypatch.setattr(close_workitem, "_capture_playwright_screenshot", AsyncMock())

        args = argparse.Namespace(timeout_seconds=30, debug_hold_seconds=0)
        targets = [
            {"mva": "011111111", "complaint_type": "Glass"},
            {"mva": "022222222", "complaint_type": "Glass"},
        ]

        results = asyncio.run(close_workitem._run_playwright_close_async(args, targets))

        assert results == [
            {
                "mva": "011111111",
                "complaint_type": "Glass",
                "result": close_workitem.RESULT_NAV_FAILED,
                "detail": "post-search contract changed",
            },
            {
                "mva": "022222222",
                "complaint_type": "Glass",
                "result": close_workitem.RESULT_NOT_ATTEMPTED,
                "detail": "batch stopped after navigation failure for 011111111",
            },
        ]
        assert navigate.await_count == 1

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

        launch_kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
        assert "chromium_sandbox" not in launch_kwargs
        assert results == [
            {
                "mva": "011111111",
                "complaint_type": "Glass",
                "result": close_workitem.RESULT_NOT_FOUND,
                "detail": "",
            },
            {
                "mva": "022222222",
                "complaint_type": "Glass",
                "result": close_workitem.RESULT_ERROR,
                "detail": "browser unavailable",
            },
            {
                "mva": "033333333",
                "complaint_type": "Glass",
                "result": close_workitem.RESULT_NOT_ATTEMPTED,
                "detail": "batch stopped after browser failure for 022222222",
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
