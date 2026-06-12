"""Browser/profile lifecycle for Compass GO scraping.

Mirrors the profile-attach pattern used by WorkItems/create_workitem.py:
launches Edge with the user's signed-in profile so SSO is reused. Sync API
(matches the legacy subprocess worker contract — entry point is a plain
`python src/CompassGoParser.py`).
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

DEFAULT_ENTRY_URL = "https://go.avisbudget.palantirfoundry.com/"
DEFAULT_EDGE_USER_DATA_DIR = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
DEFAULT_EDGE_PROFILE_DIRECTORY = "Default"
EDGE_KILL_MAX_ATTEMPTS = 3
EDGE_KILL_WAIT_S = 2
LAUNCH_RETRY_ATTEMPTS = 2


def _is_edge_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        log.warning("tasklist failed: %s", exc)
        return False
    return any(
        line.strip().lower().startswith("msedge.exe")
        for line in result.stdout.splitlines()
    )


def kill_running_edge() -> bool:
    """Release the user-data-dir lock by terminating any running Edge.

    Mirrors the WorkItems pattern: kill -> short sleep -> verify gone.
    Raises RuntimeError if Edge survives the kill so we fail fast instead of
    hitting an opaque 'profile in use' error from launch_persistent_context.
    """
    if not _is_edge_running():
        return True

    for attempt in range(1, EDGE_KILL_MAX_ATTEMPTS + 1):
        log.info(
            "Closing running Edge to release profile lock (attempt %d/%d)",
            attempt,
            EDGE_KILL_MAX_ATTEMPTS,
        )
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "msedge.exe", "/T"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            log.warning("Failed to terminate Edge processes: %s", exc)
            return False

        time.sleep(EDGE_KILL_WAIT_S)
        if not _is_edge_running():
            log.info("Edge processes cleared — proceeding with launch")
            return True

    log.warning(
        "Edge is still running after %d kill attempt(s); trying launch anyway",
        EDGE_KILL_MAX_ATTEMPTS,
    )
    return False


def _resolve_user_data_dir() -> str:
    val = os.getenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", "").strip()
    return val or str(DEFAULT_EDGE_USER_DATA_DIR)


def _resolve_profile_directory() -> str:
    val = os.getenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", "").strip()
    return val or DEFAULT_EDGE_PROFILE_DIRECTORY


def _resolve_headless() -> bool:
    return os.getenv("CGI_HEADLESS", "0").strip().lower() in {"1", "true", "yes", "on"}


class CompassGoSession:
    """Context manager that yields a Playwright Page bound to Compass GO."""

    def __init__(self, entry_url: str | None = None):
        self._entry_url = entry_url or os.getenv("COMPASS_GO_ENTRY_URL", DEFAULT_ENTRY_URL)

    @contextmanager
    def page(self) -> Iterator["Page"]:
        from playwright.sync_api import sync_playwright

        kill_running_edge()
        user_data_dir = _resolve_user_data_dir()
        profile_dir = _resolve_profile_directory()
        headless = _resolve_headless()
        log.info("Launching Edge profile: %s\\%s (headless=%s)", user_data_dir, profile_dir, headless)

        with sync_playwright() as pw:
            context = None
            last_exc: Exception | None = None
            for attempt in range(1, LAUNCH_RETRY_ATTEMPTS + 1):
                try:
                    context = pw.chromium.launch_persistent_context(
                        user_data_dir,
                        channel="msedge",
                        headless=headless,
                        args=[f"--profile-directory={profile_dir}"],
                        no_viewport=True,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - launch can fail for many runtime reasons
                    last_exc = exc
                    if attempt >= LAUNCH_RETRY_ATTEMPTS:
                        raise RuntimeError(
                            "Unable to launch Edge persistent profile after retries. "
                            "Close all Edge windows manually and retry."
                        ) from exc
                    log.warning(
                        "Edge profile launch failed (attempt %d/%d): %s",
                        attempt,
                        LAUNCH_RETRY_ATTEMPTS,
                        exc,
                    )
                    kill_running_edge()

            if context is None:
                # Defensive fallback (should never execute due raise above).
                raise RuntimeError("Unable to create Edge persistent context") from last_exc
            try:
                page = context.new_page()
                page.goto(self._entry_url, wait_until="domcontentloaded")
                yield page
            finally:
                context.close()

