import os
from datetime import UTC, datetime, timedelta

import pytest

from invoice_workflow.process_lock import (
    CoordinatorAlreadyRunningError,
    CoordinatorProcessLock,
)
from invoice_workflow.run_logging import (
    WorkflowRunLogger,
    cleanup_old_run_artifacts,
)


def test_process_lock_rejects_second_owner(tmp_path):
    lock_path = tmp_path / "workflow.lock"

    with CoordinatorProcessLock(lock_path):
        with pytest.raises(CoordinatorAlreadyRunningError):
            CoordinatorProcessLock(lock_path).acquire()

    with CoordinatorProcessLock(lock_path):
        pass


def test_run_logger_writes_context_and_redacts_secrets(tmp_path):
    with WorkflowRunLogger(tmp_path / "logs", "run-123") as run_log:
        run_log.info(
            "starting password=secret token=abc123",
            invoice="5294412",
            stage="identity",
        )
        try:
            raise RuntimeError("Cookie: session-secret")
        except RuntimeError:
            run_log.exception(
                "worker failed Authorization: Bearer private-token",
                invoice="5294412",
                stage="fieldpo_po",
            )
        log_path = run_log.path

    contents = log_path.read_text(encoding="utf-8")
    assert "run_id=run-123" in contents
    assert "invoice=5294412" in contents
    assert "stage=identity" in contents
    assert "[REDACTED]" in contents
    assert "password=secret" not in contents
    assert "abc123" not in contents
    assert "private-token" not in contents
    assert "session-secret" not in contents
    assert len(contents.splitlines()) == 2


def test_failure_screenshot_path_is_scoped_and_sanitized(tmp_path):
    with WorkflowRunLogger(tmp_path / "logs", "run-123") as run_log:
        screenshot = run_log.failure_screenshot_path("Invoice #52/94412", "Compass close")

    assert screenshot.parent == tmp_path / "logs" / "failures" / "run-123"
    assert screenshot.name.startswith("Invoice_52_94412_Compass_close_")
    assert screenshot.suffix == ".png"


def test_cleanup_deletes_only_stale_completed_invoice_artifacts(tmp_path):
    now = datetime(2026, 9, 4, tzinfo=UTC)
    root = tmp_path / "log" / "completed_invoices"
    failures = root / "failures"
    old_failure = failures / "old-run"
    current_failure = failures / "current-run"
    old_failure.mkdir(parents=True)
    current_failure.mkdir(parents=True)
    old_log = root / "old.log"
    current_log = root / "current.log"
    outside_log = tmp_path / "log" / "outside.log"
    old_log.write_text("old", encoding="utf-8")
    current_log.write_text("current", encoding="utf-8")
    outside_log.write_text("outside", encoding="utf-8")
    (old_failure / "failure.png").write_bytes(b"old")
    (current_failure / "failure.png").write_bytes(b"current")

    old_timestamp = (now - timedelta(days=16)).timestamp()
    current_timestamp = (now - timedelta(days=14)).timestamp()
    for path in (old_log, old_failure, old_failure / "failure.png"):
        os.utime(path, (old_timestamp, old_timestamp))
    for path in (current_log, current_failure, current_failure / "failure.png"):
        os.utime(path, (current_timestamp, current_timestamp))

    deleted = cleanup_old_run_artifacts(root, now=now)

    assert old_log in deleted
    assert old_failure in deleted
    assert not old_log.exists()
    assert not old_failure.exists()
    assert current_log.exists()
    assert current_failure.exists()
    assert outside_log.exists()
