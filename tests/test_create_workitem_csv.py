"""Tests for _build_create_targets Type-column handling in WorkItems/create_workitem.py."""
import sys
import csv
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def _write_csv(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.csv"
    p.write_text(content, encoding="utf-8")
    return p


class TestBuildCreateTargets:

    def _run(self, tmp_path, csv_content, extra_args=None):
        from WorkItems.create_workitem import _build_create_targets
        csv_path = _write_csv(tmp_path, csv_content)
        args = MagicMock()
        args.csv = str(csv_path)
        args.mva = None
        args.action = None
        if extra_args:
            for k, v in extra_args.items():
                setattr(args, k, v)
        return _build_create_targets(args)

    def test_glass_row_with_ws_location(self, tmp_path):
        targets = self._run(tmp_path, "mva,Type,location,action\n12345,Glass,WS,Replace\n")
        assert len(targets) == 1
        assert targets[0] == {"mva": "12345", "type": "Glass", "location": "WS", "action": "Replace"}

    def test_pm_row_is_rejected_by_glass_workflow(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(tmp_path, "mva,Type,location,action\n67890,PM,,\n")

    def test_missing_type_defaults_to_glass(self, tmp_path):
        targets = self._run(tmp_path, "mva,Type,location,action\n11111,,WS,Replace\n")
        assert targets[0]["type"] == "Glass"

    def test_comment_lines_skipped(self, tmp_path):
        csv_content = (
            "# this is a comment\n"
            "mva,Type,location,action\n"
            "# another comment\n"
            "22222,Glass,WS,Replace\n"
        )
        targets = self._run(tmp_path, csv_content)
        assert len(targets) == 1
        assert targets[0]["mva"] == "22222"

    def test_blank_mva_skipped(self, tmp_path):
        targets = self._run(tmp_path, "mva,Type,location,action\n,Glass,WS,Replace\n33333,Glass,WS,Replace\n")
        assert len(targets) == 1
        assert targets[0]["mva"] == "33333"

    def test_invalid_type_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(tmp_path, "mva,Type,location,action\n44444,Tires,,\n")

    def test_glass_missing_location_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(tmp_path, "mva,Type,location,action\n55555,Glass,,Replace\n")
