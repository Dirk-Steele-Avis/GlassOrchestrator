from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "Run-EnsureGlassWorkItems.cmd"


def test_consolidated_launcher_is_the_only_create_launcher():
    assert LAUNCHER.exists()
    assert not (ROOT / "Run-CreateCompassComplaints.cmd").exists()
    assert not (ROOT / "Run-CreateWorkItems.cmd").exists()


def test_no_arguments_default_to_spreadsheet_workflow():
    content = LAUNCHER.read_text(encoding="utf-8")

    assert '"%VENV_PY%" ".\\create_compass_complaints.py" %*' in content
    assert 'if /i "%~1"=="--csv" goto :run_csv' in content


def test_csv_mode_routes_to_existing_manual_workflow():
    content = LAUNCHER.read_text(encoding="utf-8")

    assert '"%VENV_PY%" WorkItems\\create_workitem.py --csv "%~2" --backend playwright' in content
    assert 'if "%~2"==""' in content
    assert 'if not exist "%~2"' in content