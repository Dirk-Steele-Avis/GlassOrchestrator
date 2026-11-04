"""Build the script-usage survey report from per-process coverage data files.

Each ``.coverage.*`` parallel data file corresponds to one Python process
recorded by the dead-code survey (see Run-CoverageSurvey.cmd). This module
attributes each process to the launcher script it executed and renders an
HTML report with a per-script summary and 24/7 (day x hour) run-count
heatmaps for the survey window.

A script counts as "run" only when a statement inside its
``if __name__ == "__main__":`` block executed, so imports of a script by
another entry point are not miscounted as runs. Scripts without a main
guard are counted on any executed line and flagged in the report.
"""

from __future__ import annotations

import argparse
import ast
import html
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from coverage import CoverageData

ROOT = Path(__file__).resolve().parent
REPORT_NAME = "coverage_survey_report.html"
SELF_SCRIPT = Path(__file__).name

_PY_TARGET_RE = re.compile(r"(?i)\b([\w][\w\\/.-]*\.py)\b")
_MODULE_TARGET_RE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")


@dataclass(frozen=True)
class ScriptProfile:
    """A launcher-reachable script and its run-detection line numbers."""

    rel_path: str  # posix-style path relative to the project root
    guard_lines: frozenset[int]  # empty when the script has no main guard


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


def discover_launcher_scripts(root: Path) -> list[str]:
    """Return scripts referenced by Run-*.cmd launchers, as relative posix paths."""
    found: set[str] = set()
    launchers = sorted(root.glob("Run-*.cmd"))
    if not launchers:
        raise RuntimeError(f"No Run-*.cmd launchers found under {root}")
    for launcher in launchers:
        text = launcher.read_text(encoding="utf-8", errors="replace")
        for match in _PY_TARGET_RE.finditer(text):
            rel = _normalize(match.group(1)).lstrip("./")
            if rel == SELF_SCRIPT:
                continue
            if (root / rel).is_file():
                found.add(rel)
        for match in _MODULE_TARGET_RE.finditer(text):
            rel = match.group(1).replace(".", "/") + ".py"
            if (root / rel).is_file():
                found.add(rel)
    return sorted(found)


def main_guard_body_lines(source: str) -> frozenset[int]:
    """Return line numbers of top-level statements inside the main guard."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare):
            names = [test.left, *test.comparators]
            if any(isinstance(n, ast.Name) and n.id == "__name__" for n in names):
                return frozenset(stmt.lineno for stmt in node.body)
    return frozenset()


def build_profiles(root: Path, scripts: list[str]) -> list[ScriptProfile]:
    profiles = []
    for rel in scripts:
        source = (root / rel).read_text(encoding="utf-8", errors="replace")
        profiles.append(ScriptProfile(rel, main_guard_body_lines(source)))
    return profiles


def _module_candidates(root: Path, name: str, importer: Path) -> list[tuple[Path, Path]]:
    """Possible (base, file) pairs for a dotted module name."""
    rel = name.replace(".", "/")
    candidates = []
    for base in (root, importer.parent):
        candidates.append((base, base / f"{rel}.py"))
        candidates.append((base, base / rel / "__init__.py"))
    return candidates


def _imports_of(root: Path, path: Path) -> set[Path]:
    """Local files imported by one module, resolved best-effort via ast."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found: set[Path] = set()

    def _resolve(name: str) -> None:
        for base, candidate in _module_candidates(root, name, path):
            if candidate.is_file():
                found.add(candidate.resolve())
                # Importing a module also executes every ancestor package
                # __init__.py, so mark those reachable too.
                parent = candidate.parent
                while parent != base and (parent / "__init__.py").is_file():
                    found.add((parent / "__init__.py").resolve())
                    parent = parent.parent
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _resolve(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                prefix = base.relative_to(root).as_posix().replace("/", ".")
                module = f"{prefix}.{node.module}" if node.module else prefix
            else:
                module = node.module or ""
            if module:
                _resolve(module)
                for alias in node.names:
                    _resolve(f"{module}.{alias.name}")

    return found


def reachable_files(root: Path, scripts: list[str]) -> set[Path]:
    """All local .py files transitively imported from the launcher scripts."""
    queue = [(root / rel).resolve() for rel in scripts]
    seen: set[Path] = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(_imports_of(root, current))
    return seen


def unreachable_files(root: Path, scripts: list[str]) -> list[str]:
    """Repo .py files no launcher or Copilot skill can reach, as relative posix paths."""
    # Skill scripts are entry points too (run via the venv, not a launcher);
    # their imports must not be reported as orphans.
    skill_scripts = [
        p.relative_to(root).as_posix()
        for p in root.glob(".github/skills/*/scripts/*.py")
    ]
    reachable = reachable_files(root, scripts + skill_scripts)
    skip_parts = {".venv", "tests", "__pycache__", ".github", ".git"}
    orphans = []
    for path in root.rglob("*.py"):
        resolved = path.resolve()
        rel = resolved.relative_to(root)
        if skip_parts & set(rel.parts):
            continue
        if resolved not in reachable and rel.name != SELF_SCRIPT:
            orphans.append(rel.as_posix())
    return sorted(orphans)


def classify_data_file(data_path: Path, profiles: list[ScriptProfile]) -> list[str]:
    """Return the scripts this process ran, judged by main-guard execution."""
    data = CoverageData(basename=str(data_path))
    data.read()
    measured = {_normalize(f): f for f in data.measured_files()}
    ran: list[str] = []
    for profile in profiles:
        stored = measured.get(profile.rel_path)
        if stored is None:
            continue
        executed = set(data.lines(stored) or ())
        if profile.guard_lines:
            if profile.guard_lines & executed:
                ran.append(profile.rel_path)
        elif executed:
            ran.append(profile.rel_path)
    return ran


def collect_usage(
    root: Path, data_dir: Path, profiles: list[ScriptProfile]
) -> dict[str, list[datetime]]:
    """Map each script to the finish timestamps of its recorded runs."""
    usage: dict[str, list[datetime]] = {p.rel_path: [] for p in profiles}
    for data_path in sorted(data_dir.glob(".coverage.*")):
        finished = datetime.fromtimestamp(data_path.stat().st_mtime)
        for rel in classify_data_file(data_path, profiles):
            usage[rel].append(finished)
    return usage


def _heat_cell(count: int, max_count: int) -> str:
    if count == 0:
        return '<td class="z">&middot;</td>'
    alpha = 0.25 + 0.75 * (count / max_count)
    return f'<td style="background:rgba(21,128,61,{alpha:.2f})">{count}</td>'


def _heatmap_table(runs: list[datetime], first_day, last_day) -> str:
    counts: dict[tuple, int] = {}
    for ts in runs:
        key = (ts.date(), ts.hour)
        counts[key] = counts.get(key, 0) + 1
    max_count = max(counts.values(), default=0)
    head = "".join(f"<th>{h:02d}</th>" for h in range(24))
    rows = []
    day = first_day
    while day <= last_day:
        cells = "".join(
            _heat_cell(counts.get((day, hour), 0), max_count) for hour in range(24)
        )
        rows.append(f"<tr><th>{day.strftime('%a %m/%d')}</th>{cells}</tr>")
        day += timedelta(days=1)
    return (
        '<table class="heat"><tr><th></th>' + head + "</tr>" + "".join(rows) + "</table>"
    )


def render_report(
    usage: dict[str, list[datetime]],
    profiles: list[ScriptProfile],
    orphans: list[str],
    generated: datetime,
) -> str:
    guardless = {p.rel_path for p in profiles if not p.guard_lines}
    all_runs = [ts for runs in usage.values() for ts in runs]
    if all_runs:
        first_day = min(ts.date() for ts in all_runs)
        last_day = max(ts.date() for ts in all_runs)
    else:
        first_day = last_day = generated.date()

    summary_rows = []
    for rel in sorted(usage, key=lambda r: (len(usage[r]), r)):
        runs = usage[rel]
        name = html.escape(rel) + (" &dagger;" if rel in guardless else "")
        if runs:
            last = max(runs).strftime("%a %m/%d %H:%M")
            status = f"used ({len(runs)} runs)"
            row_class = ""
        else:
            last = "&mdash;"
            status = "UNUSED"
            row_class = ' class="unused"'
        summary_rows.append(
            f"<tr{row_class}><td>{name}</td><td>{len(runs)}</td>"
            f"<td>{last}</td><td>{status}</td></tr>"
        )

    sections = [
        "<h2>Overall activity (all scripts)</h2>",
        _heatmap_table(all_runs, first_day, last_day),
    ]
    for rel in sorted(usage, key=lambda r: (-len(usage[r]), r)):
        if usage[rel]:
            sections.append(f"<h2>{html.escape(rel)}</h2>")
            sections.append(_heatmap_table(usage[rel], first_day, last_day))

    footnote = (
        "<p>&dagger; No <code>if __name__ == \"__main__\"</code> guard: "
        "any execution of the file is counted as a run.</p>"
        if guardless
        else ""
    )
    if orphans:
        orphan_items = "".join(f"<li>{html.escape(o)}</li>" for o in orphans)
        orphan_section = (
            "<h2>Not reachable from any launcher</h2>"
            "<p>These .py files are neither referenced by a Run-*.cmd launcher "
            "nor imported by anything a launcher runs &mdash; orphan candidates "
            "regardless of the survey window.</p>"
            f'<ul class="orphans">{orphan_items}</ul>'
        )
    else:
        orphan_section = ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Script Usage Survey</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #1f2937; }}
table {{ border-collapse: collapse; margin-bottom: 24px; }}
td, th {{ border: 1px solid #d1d5db; padding: 4px 8px; font-size: 13px; }}
.heat td {{ width: 26px; text-align: center; padding: 2px; }}
.heat td.z {{ color: #d1d5db; }}
tr.unused td {{ background: #fee2e2; font-weight: bold; }}
ul.orphans li {{ color: #b91c1c; font-family: Consolas, monospace; }}
</style></head><body>
<h1>Script Usage Survey</h1>
<p>Generated {generated.strftime('%Y-%m-%d %H:%M')} &mdash;
survey window {first_day.strftime('%m/%d')} to {last_day.strftime('%m/%d')}.
Each run is one recorded Python process attributed to its entry script.</p>
<h2>Summary</h2>
<table><tr><th>Script</th><th>Runs</th><th>Last run</th><th>Status</th></tr>
{''.join(summary_rows)}</table>
{footnote}
{orphan_section}
{''.join(sections)}
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    data_dir = (args.data_dir or root).resolve()
    output = args.output or root / "reports" / REPORT_NAME

    scripts = discover_launcher_scripts(root)
    profiles = build_profiles(root, scripts)
    usage = collect_usage(root, data_dir, profiles)
    orphans = unreachable_files(root, scripts)

    total = sum(len(v) for v in usage.values())
    unused = [rel for rel, runs in usage.items() if not runs]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(usage, profiles, orphans, datetime.now()), encoding="utf-8"
    )

    print(f"Scripts surveyed: {len(usage)}  attributed runs: {total}")
    for rel in sorted(unused):
        print(f"UNUSED: {rel}")
    for rel in orphans:
        print(f"ORPHAN (unreachable from launchers): {rel}")
    print(f"Script usage report: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
