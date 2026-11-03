from pathlib import Path
import os
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_intake_wrapper_calls_expected_steps() -> None:
    content = _read("Run-Glass-Intake.cmd")
    assert "glass_intake.py" in content
    assert "GlassOrchestrator.py" not in content
    assert "FieldPOFillNextAction.py" not in content
    assert "create_compass_complaints.py" not in content
    assert "continuing to EnsureGlassWorkItems" not in content


def test_standalone_wrappers_require_explicit_targets() -> None:
    fieldpo = _read("Run-FieldPOFillNextAction.cmd")
    compass = _read("Run-EnsureGlassWorkItems.cmd")
    assert "--inventory-date YYYY-MM-DD" in fieldpo
    assert "Explicit target required" in fieldpo
    assert "--inventory-date YYYY-MM-DD" in compass
    assert "Explicit target required" in compass


def test_closeout_wrapper_supports_mva_and_batch_modes() -> None:
    content = _read("Run-Glass-Closeout.cmd")
    assert "build_close_queue.py" in content
    assert "close_workitem.py" in content
    assert "invoice_workflow.cli" in content
    assert "--mva" in content
    assert "--csv" in content
    assert "--max-invoices" in content


def test_closeout_wrapper_passes_invoice_work_item_results() -> None:
    content = _read("Run-Glass-Closeout.cmd")
    assert content.count("--concise-console") == 5
    assert content.count("--results-json data\\glass_closeout_work_items.json") == 5
    assert "--work-item-results data\\glass_closeout_work_items.json" in content


def _run_closeout_wrapper(tmp_path, arguments, exit_code=0):
    wrapper = tmp_path / "Run-Glass-Closeout.cmd"
    wrapper.write_text(_read(wrapper.name), encoding="utf-8")
    (tmp_path / "Run-GlassBootstrap.cmd").write_text(
        '@echo off\n'
        'echo %*>> "%~dp0calls.txt"\n'
        f'exit /b {exit_code}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [os.environ["ComSpec"], "/d", "/c", str(wrapper), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    calls_path = tmp_path / "calls.txt"
    calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
    return result, calls


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-invoices", "1"],
        ["--Max", "1"],
        ["--invoice-number", "5485994"],
        ["--max-invoices", "1", "--invoice-number", "5485994"],
    ],
)
def test_limited_closeout_bypasses_unbounded_steps(tmp_path, arguments):
    result, calls = _run_closeout_wrapper(tmp_path, arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1
    assert "-m invoice_workflow.cli --close-work-items" in calls[0]
    assert "build_close_queue.py" not in calls[0]
    assert "WorkItems\\close_workitem.py" not in calls[0]
    if "--invoice-number" in arguments:
        assert "--invoice-number 5485994" in calls[0]
    if "--max-invoices" in arguments or "--Max" in arguments:
        assert "--max-invoices 1" in calls[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
@pytest.mark.parametrize(
    "extra",
    [["--mva", "59235573"], ["--csv", "review.csv"], ["--max-rows", "1"],
     ["--sweep-terminal-to-needs-review"]],
)
def test_limited_closeout_rejects_conflicting_scope_before_bootstrap(tmp_path, extra):
    result, calls = _run_closeout_wrapper(tmp_path, ["--max-invoices", "1", *extra])
    assert result.returncode == 2
    assert "cannot be combined" in result.stdout
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
@pytest.mark.parametrize("arguments", [[], ["--max-invoices", "1"]])
def test_closeout_stops_and_preserves_stage_failure_exit_code(tmp_path, arguments):
    result, calls = _run_closeout_wrapper(tmp_path, arguments, exit_code=7)
    assert result.returncode == 7
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_unlimited_closeout_keeps_existing_three_stage_order(tmp_path):
    result, calls = _run_closeout_wrapper(tmp_path, [])
    assert result.returncode == 0
    assert len(calls) == 3
    assert "build_close_queue.py --apply" in calls[0]
    assert "WorkItems\\close_workitem.py" in calls[1]
    assert "-m invoice_workflow.cli" in calls[2]
    assert "--close-work-items" not in calls[2]


def test_shared_bootstrap_exists() -> None:
    content = _read("Run-GlassBootstrap.cmd")
    assert "Installing Playwright browsers" in content
    assert "call %*" in content


def test_fieldpo_closer_wrapper_forwards_explicit_mode() -> None:
    content = _read("Run-FieldPO_Closer.cmd")
    assert "FieldPO_Closer.py %*" in content


def test_morning_report_wrapper_generates_report_without_emailing() -> None:
    content = _read("Run-Glass-Morning-Report.cmd")
    assert "glass_morning_report.py" in content
    assert "Run-GlassBootstrap.cmd" in content
    assert "Copy report for Outlook" in content
    assert "send" not in content.lower()