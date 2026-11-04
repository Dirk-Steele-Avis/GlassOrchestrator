from datetime import date

import pytest

import glass_intake


def test_process_inventory_date_runs_fieldpo_before_compass(monkeypatch):
    calls = []
    target_date = date(2026, 9, 25)

    monkeypatch.setattr(
        glass_intake.FieldPOFillNextAction,
        "run",
        lambda target_inventory_date: calls.append(("fieldpo", target_inventory_date)),
    )
    monkeypatch.setattr(
        glass_intake.create_compass_complaints,
        "run",
        lambda target_inventory_date: calls.append(("compass", target_inventory_date)) or 0,
    )

    assert glass_intake._process_inventory_date(target_date) == 0
    assert calls == [("fieldpo", target_date), ("compass", target_date)]


def test_process_inventory_date_stops_when_fieldpo_fails(monkeypatch):
    compass_calls = []

    def fail_fieldpo(target_inventory_date):
        raise RuntimeError(f"failed {target_inventory_date}")

    monkeypatch.setattr(glass_intake.FieldPOFillNextAction, "run", fail_fieldpo)
    monkeypatch.setattr(
        glass_intake.create_compass_complaints,
        "run",
        lambda target_inventory_date: compass_calls.append(target_inventory_date) or 0,
    )

    with pytest.raises(RuntimeError, match="failed 2026-09-25"):
        glass_intake._process_inventory_date(date(2026, 9, 25))

    assert compass_calls == []


def test_process_inventory_date_fails_when_compass_returns_nonzero(monkeypatch):
    monkeypatch.setattr(
        glass_intake.FieldPOFillNextAction,
        "run",
        lambda target_inventory_date: None,
    )
    monkeypatch.setattr(
        glass_intake.create_compass_complaints,
        "run",
        lambda target_inventory_date: 1,
    )

    with pytest.raises(RuntimeError, match="Compass processing failed"):
        glass_intake._process_inventory_date(date(2026, 9, 25))


def test_run_passes_date_callback_to_pipeline(monkeypatch):
    captured = {}

    def fake_pipeline(after_batch):
        captured["callback"] = after_batch
        return 7

    monkeypatch.setattr(glass_intake, "run_pipeline", fake_pipeline)

    assert glass_intake.run() == 7
    assert captured["callback"] is glass_intake._process_inventory_date