from datetime import datetime
from pathlib import Path

from coverage import CoverageData

from coverage_survey_report import (
    ScriptProfile,
    build_profiles,
    classify_data_file,
    collect_usage,
    discover_launcher_scripts,
    main_guard_body_lines,
    render_report,
    unreachable_files,
)


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_data(path: Path, lines_by_file: dict[str, set[int]]) -> None:
    data = CoverageData(basename=str(path))
    data.add_lines({f: sorted(lines) for f, lines in lines_by_file.items()})
    data.write()


def test_discover_launcher_scripts_parses_py_and_module_targets(tmp_path) -> None:
    _write(tmp_path, "alpha.py")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/cli.py")
    _write(
        tmp_path,
        "Run-Alpha.cmd",
        'python.exe ".\\alpha.py"\npython.exe -m pkg.cli --close\n',
    )
    _write(tmp_path, "Run-Missing.cmd", "python.exe does_not_exist.py\n")

    assert discover_launcher_scripts(tmp_path) == ["alpha.py", "pkg/cli.py"]


def test_main_guard_body_lines_finds_guard_statements() -> None:
    source = (
        "import sys\n"
        "def main():\n"
        "    return 0\n"
        "if __name__ == \"__main__\":\n"
        "    code = main()\n"
        "    sys.exit(code)\n"
    )
    assert main_guard_body_lines(source) == frozenset({5, 6})
    assert main_guard_body_lines("import sys\nprint('hi')\n") == frozenset()


def test_classify_requires_main_guard_execution(tmp_path) -> None:
    profiles = [
        ScriptProfile("alpha.py", frozenset({10})),
        ScriptProfile("beta.py", frozenset({20})),
    ]
    data_path = tmp_path / ".coverage.host.pid1.x"
    # alpha ran (guard body line 10 executed); beta was only imported.
    _write_data(data_path, {"alpha.py": {1, 2, 10}, "beta.py": {1, 2}})

    assert classify_data_file(data_path, profiles) == ["alpha.py"]


def test_classify_counts_guardless_script_on_any_execution(tmp_path) -> None:
    profiles = [ScriptProfile("gamma.py", frozenset())]
    data_path = tmp_path / ".coverage.host.pid2.x"
    _write_data(data_path, {"gamma.py": {1}})

    assert classify_data_file(data_path, profiles) == ["gamma.py"]


def test_collect_usage_counts_runs_per_script(tmp_path) -> None:
    profiles = [
        ScriptProfile("alpha.py", frozenset({10})),
        ScriptProfile("beta.py", frozenset({20})),
    ]
    _write_data(tmp_path / ".coverage.host.pid1.x", {"alpha.py": {10}})
    _write_data(tmp_path / ".coverage.host.pid2.x", {"alpha.py": {10}})
    _write_data(tmp_path / ".coverage.host.pid3.x", {"alpha.py": {1}})

    usage = collect_usage(tmp_path, tmp_path, profiles)

    assert len(usage["alpha.py"]) == 2
    assert usage["beta.py"] == []


def test_unreachable_files_reports_only_orphans(tmp_path) -> None:
    _write(tmp_path, "Run-Alpha.cmd", "python.exe alpha.py\n")
    _write(tmp_path, "alpha.py", "import helper\nfrom pkg import mod\n")
    _write(tmp_path, "helper.py")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/mod.py")
    _write(tmp_path, "orphan.py")
    _write(tmp_path, "tests/test_orphan.py", "import orphan\n")
    _write(
        tmp_path,
        ".github/skills/demo/scripts/skill.py",
        "import skill_helper\n",
    )
    _write(tmp_path, "skill_helper.py")

    assert unreachable_files(tmp_path, ["alpha.py"]) == ["orphan.py"]


def test_unreachable_files_marks_package_init_reachable(tmp_path) -> None:
    _write(tmp_path, "Run-Alpha.cmd", "python.exe alpha.py\n")
    _write(tmp_path, "alpha.py", "import pkg.sub.mod\n")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/sub/__init__.py")
    _write(tmp_path, "pkg/sub/mod.py")

    assert unreachable_files(tmp_path, ["alpha.py"]) == []


def test_build_profiles_reads_guards_from_disk(tmp_path) -> None:
    _write(
        tmp_path,
        "alpha.py",
        "if __name__ == \"__main__\":\n    print('run')\n",
    )
    profiles = build_profiles(tmp_path, ["alpha.py"])
    assert profiles == [ScriptProfile("alpha.py", frozenset({2}))]


def test_render_report_shows_unused_orphans_and_counts() -> None:
    profiles = [
        ScriptProfile("alpha.py", frozenset({10})),
        ScriptProfile("beta.py", frozenset()),
    ]
    usage = {
        "alpha.py": [datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 8, 30)],
        "beta.py": [],
    }
    html_text = render_report(
        usage, profiles, ["orphan.py"], datetime(2026, 11, 4, 9, 0)
    )

    assert "UNUSED" in html_text
    assert "beta.py" in html_text
    assert "orphan.py" in html_text
    assert "Not reachable from any launcher" in html_text
    # Both alpha runs land in the same day/hour heatmap cell.
    assert ">2</td>" in html_text
    # beta.py has no main guard and is flagged with the dagger footnote.
    assert "&dagger;" in html_text
