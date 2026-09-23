from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_intake_wrapper_calls_expected_steps() -> None:
    content = _read("Run-Glass-Intake.cmd")
    assert "GlassOrchestrator.py" in content
    assert "FieldPOFillNextAction.py" in content
    assert "create_compass_complaints.py" in content
    assert "continuing to EnsureGlassWorkItems" in content


def test_closeout_wrapper_supports_mva_and_batch_modes() -> None:
    content = _read("Run-Glass-Closeout.cmd")
    assert "build_close_queue.py" in content
    assert "close_workitem.py" in content
    assert "invoice_workflow.cli" in content
    assert "--mva" in content
    assert "--csv" in content
    assert "--max-invoices" in content


def test_shared_bootstrap_exists() -> None:
    content = _read("Run-GlassBootstrap.cmd")
    assert "Installing Playwright browsers" in content
    assert "call %*" in content


def test_morning_report_wrapper_generates_report_without_emailing() -> None:
    content = _read("Run-Glass-Morning-Report.cmd")
    assert "glass_morning_report.py" in content
    assert "Run-GlassBootstrap.cmd" in content
    assert "Copy report for Outlook" in content
    assert "send" not in content.lower()