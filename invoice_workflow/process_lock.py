"""Exclusive process lock for the completed-invoice coordinator."""

from __future__ import annotations

import msvcrt
from pathlib import Path
from typing import BinaryIO


class CoordinatorAlreadyRunningError(RuntimeError):
    """Raised when another completed-invoice coordinator owns the lock."""


class CoordinatorProcessLock:
    """Hold a non-blocking Windows byte-range lock for one coordinator run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("Coordinator process lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            lock_file.close()
            raise CoordinatorAlreadyRunningError(
                f"Another completed-invoice coordinator owns {self.path}"
            ) from exc
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> CoordinatorProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
