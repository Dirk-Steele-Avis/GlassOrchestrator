"""
Unit tests — CSV location validation in WorkItems.create_workitem._build_create_targets().

Valid location values are glass area codes configured in orchestrator_config.json.
Lot codes such as BB and APO are not valid and must be rejected before
any browser automation starts.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_args(csv_path: str):
    args = MagicMock()
    args.csv = csv_path
    args.mva = None
    args.action = None
    return args


def _write_csv(tmp_path: Path, rows: list[str]) -> str:
    p = tmp_path / "test.csv"
    p.write_text("mva,location,action\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return str(p)


class TestLocationValidation:
    """_build_create_targets rejects rows whose location is not a known glass area code."""

    def test_lot_code_bb_is_rejected(self, tmp_path):
        """BB is a lot location code, not a glass area — must be caught before browser launch."""
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,BB,Replace"])
        with pytest.raises(SystemExit):
            _build_create_targets(_make_args(csv))

    def test_lot_code_apo_is_rejected(self, tmp_path):
        """APO is a request location code, not a glass area."""
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,APO,Replace"])
        with pytest.raises(SystemExit):
            _build_create_targets(_make_args(csv))

    def test_ws_is_accepted(self, tmp_path):
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,WS,Replace"])
        targets = _build_create_targets(_make_args(csv))
        assert len(targets) == 1

    def test_side_window_codes_are_accepted(self, tmp_path):
        """Directional codes use side + orientation + area order."""
        from WorkItems.create_workitem import _build_create_targets

        side_codes = ["LFD", "RFD", "LRD", "RRD", "LFV", "RFV", "LRQ", "RRQ", "RFW"]
        rows = [f"5900000{i},{code},Replace" for i, code in enumerate(side_codes)]
        csv = _write_csv(tmp_path, rows)
        targets = _build_create_targets(_make_args(csv))
        assert len(targets) == len(side_codes)

    def test_legacy_orientation_first_code_is_normalized(self, tmp_path):
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,FLD,Replace"])
        targets = _build_create_targets(_make_args(csv))
        assert targets[0]["location"] == "LFD"

    def test_camera_is_accepted_from_config(self, tmp_path):
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,cam,Replace"])
        targets = _build_create_targets(_make_args(csv))
        assert targets[0]["location"] == "CAM"

    def test_rvm_is_accepted(self, tmp_path):
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,RVM,Replace"])
        targets = _build_create_targets(_make_args(csv))
        assert targets[0]["location"] == "RVM"

    def test_location_check_is_case_insensitive(self, tmp_path):
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,ws,Replace"])
        targets = _build_create_targets(_make_args(csv))
        assert len(targets) == 1

    def test_invalid_location_error_message_names_the_bad_value(self, tmp_path, caplog):
        """The error log must name the invalid location so the user knows what to fix."""
        import logging
        from WorkItems.create_workitem import _build_create_targets

        csv = _write_csv(tmp_path, ["59000001,BB,Replace"])
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit):
                _build_create_targets(_make_args(csv))

        assert "BB" in caplog.text
