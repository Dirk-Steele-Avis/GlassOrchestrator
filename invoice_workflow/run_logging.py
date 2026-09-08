"""Per-run review logging and retention for completed invoices."""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RETENTION_DAYS = 15

_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
)


def redact(value: object) -> str:
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class _RedactingSingleLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = redact(super().format(record))
        return formatted.replace("\r\n", "\\n").replace("\n", "\\n")


class WorkflowRunLogger:
    """Write one contextual UTF-8 review log for a coordinator invocation."""

    def __init__(self, root: str | Path, run_id: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_id = run_id
        self.path = self.root / f"{timestamp}_{run_id}.log"
        self._logger = logging.getLogger(f"completed_invoices.{run_id}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        self._logger.handlers.clear()
        handler = logging.FileHandler(self.path, encoding="utf-8")
        handler.setFormatter(
            _RedactingSingleLineFormatter(
                "%(asctime)sZ level=%(levelname)s run_id=%(run_id)s "
                "invoice=%(invoice)s stage=%(stage)s event=%(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        self._logger.addHandler(handler)

    def info(self, event: str, *, invoice: str = "-", stage: str = "run") -> None:
        self._write(logging.INFO, event, invoice=invoice, stage=stage)

    def error(self, event: str, *, invoice: str = "-", stage: str = "run") -> None:
        self._write(logging.ERROR, event, invoice=invoice, stage=stage)

    def exception(
        self,
        event: str,
        *,
        invoice: str = "-",
        stage: str = "run",
    ) -> None:
        self._write(
            logging.ERROR,
            event,
            invoice=invoice,
            stage=stage,
            exc_info=True,
        )

    def _write(
        self,
        level: int,
        event: str,
        *,
        invoice: str,
        stage: str,
        exc_info: bool = False,
    ) -> None:
        extra: dict[str, Any] = {
            "run_id": self.run_id,
            "invoice": invoice or "-",
            "stage": stage or "run",
        }
        self._logger.log(level, event, extra=extra, exc_info=exc_info)

    def failure_screenshot_path(self, invoice: str, stage: str) -> Path:
        failure_dir = self.root / "failures" / self.run_id
        failure_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return failure_dir / (
            f"{_safe_component(invoice)}_{_safe_component(stage)}_{timestamp}.png"
        )

    def close(self) -> None:
        handlers = list(self._logger.handlers)
        for handler in handlers:
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)

    def __enter__(self) -> WorkflowRunLogger:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def cleanup_old_run_artifacts(
    root: str | Path,
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
) -> list[Path]:
    """Delete stale run logs and failure directories only beneath root."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    root_path = Path(root).resolve()
    if not root_path.exists():
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    deleted: list[Path] = []

    for candidate in root_path.glob("*.log"):
        if _modified_at(candidate) < cutoff:
            candidate.unlink()
            deleted.append(candidate)

    failures_root = root_path / "failures"
    if failures_root.is_dir():
        for run_directory in failures_root.iterdir():
            if not run_directory.is_dir():
                continue
            if _modified_at(run_directory) < cutoff:
                shutil.rmtree(run_directory)
                deleted.append(run_directory)
    return deleted


def _modified_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return normalized[:80] or "unknown"
