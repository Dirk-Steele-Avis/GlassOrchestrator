import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter


def _load_xml(xml_path: str):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    suite = root
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
        if not suites:
            raise RuntimeError(f"No testsuite nodes in {xml_path}")
        suite = suites[0]

    totals = {
        "tests": int(suite.attrib.get("tests", 0)),
        "failures": int(suite.attrib.get("failures", 0)),
        "errors": int(suite.attrib.get("errors", 0)),
        "skipped": int(suite.attrib.get("skipped", 0)),
        "time": float(suite.attrib.get("time", 0.0)),
    }

    failed = set()
    skipped = set()
    passed = set()
    failure_types = Counter()

    for case in suite.findall("testcase"):
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        nodeid = f"{classname}::{name}" if classname else name

        failure_node = case.find("failure")
        error_node = case.find("error")
        skipped_node = case.find("skipped")

        if failure_node is not None:
            failed.add(nodeid)
            failure_types[failure_node.attrib.get("type", "failure")] += 1
        elif error_node is not None:
            failed.add(nodeid)
            failure_types[error_node.attrib.get("type", "error")] += 1
        elif skipped_node is not None:
            skipped.add(nodeid)
        else:
            passed.add(nodeid)

    return {
        "totals": totals,
        "failed": failed,
        "skipped": skipped,
        "passed": passed,
        "failure_types": dict(failure_types),
    }


def _resolve_xml_path(path_or_dir: str) -> str:
    if os.path.isdir(path_or_dir):
        candidate = os.path.join(path_or_dir, "pytest-results.xml")
        if os.path.exists(candidate):
            return candidate
        raise FileNotFoundError(f"Missing pytest-results.xml in {path_or_dir}")
    return path_or_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two pytest junit baselines")
    parser.add_argument("baseline_a", help="Path to baseline A directory or pytest-results.xml")
    parser.add_argument("baseline_b", help="Path to baseline B directory or pytest-results.xml")
    parser.add_argument("--out", help="Optional output JSON path")
    args = parser.parse_args()

    xml_a = _resolve_xml_path(args.baseline_a)
    xml_b = _resolve_xml_path(args.baseline_b)

    a = _load_xml(xml_a)
    b = _load_xml(xml_b)

    result = {
        "baseline_a": xml_a,
        "baseline_b": xml_b,
        "totals_a": a["totals"],
        "totals_b": b["totals"],
        "new_failures": sorted(b["failed"] - a["failed"]),
        "resolved_failures": sorted(a["failed"] - b["failed"]),
        "new_skips": sorted(b["skipped"] - a["skipped"]),
        "resolved_skips": sorted(a["skipped"] - b["skipped"]),
        "failure_types_a": a["failure_types"],
        "failure_types_b": b["failure_types"],
    }

    result["parity_ok"] = (
        result["totals_a"]["tests"] == result["totals_b"]["tests"]
        and result["totals_a"]["failures"] == result["totals_b"]["failures"]
        and result["totals_a"]["errors"] == result["totals_b"]["errors"]
        and result["totals_a"]["skipped"] == result["totals_b"]["skipped"]
        and not result["new_failures"]
        and not result["new_skips"]
    )

    output = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
    print(output)

    return 0 if result["parity_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
