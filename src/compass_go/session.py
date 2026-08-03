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
DEFAULT_AUTOMATION_USER_DATA_DIR = Path(__file__).resolve().parents[2] / ".edge-playwright-profile"
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
        "Edge is still running after %d kill attempt(s)",
        EDGE_KILL_MAX_ATTEMPTS,
    )
    return False


def _resolve_user_data_dir() -> str:
    val = os.getenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", "").strip()
    # Default to a dedicated automation profile so persistent mode can attach
    # reliably (Edge blocks DevTools attach on the default user data dir).
    return val or str(DEFAULT_AUTOMATION_USER_DATA_DIR)


def _resolve_profile_directory() -> str:
    val = os.getenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", "").strip()
    return val or DEFAULT_EDGE_PROFILE_DIRECTORY


def _resolve_headless() -> bool:
    return os.getenv("CGI_HEADLESS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _is_default_user_data_dir(user_data_dir: str) -> bool:
    try:
        return Path(user_data_dir).resolve() == DEFAULT_EDGE_USER_DATA_DIR.resolve()
    except Exception:
        return user_data_dir == str(DEFAULT_EDGE_USER_DATA_DIR)


def _should_use_persistent_context(user_data_dir: str) -> bool:
    force = os.getenv("COMPASS_GO_FORCE_PERSISTENT", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return True
    return not _is_default_user_data_dir(user_data_dir)


class CompassGoSession:
    """Context manager that yields a Playwright Page bound to Compass GO."""

    def __init__(self, entry_url: str | None = None):
        self._entry_url = entry_url or os.getenv("COMPASS_GO_ENTRY_URL", DEFAULT_ENTRY_URL)

    @staticmethod
    def _wire_lifecycle_logging(browser, context) -> None:
        def _on_browser_disconnected() -> None:
            log.warning("Browser event: disconnected")

        def _on_context_close() -> None:
            log.warning("Context event: closed")

        def _on_page(page) -> None:
            try:
                log.info("Context event: page created (url=%s)", page.url)
            except Exception:
                log.info("Context event: page created (url unavailable)")

            def _on_page_close() -> None:
                try:
                    log.warning("Page event: closed (url=%s)", page.url)
                except Exception:
                    log.warning("Page event: closed (url unavailable)")

            page.on("close", lambda: _on_page_close())

        browser.on("disconnected", lambda: _on_browser_disconnected())
        context.on("close", lambda: _on_context_close())
        context.on("page", _on_page)

        # Also attach close logging to pages that already exist.
        try:
            existing_pages = list(context.pages)
        except Exception:
            existing_pages = []
        for p in existing_pages:
            _on_page(p)

    @contextmanager
    def page(self) -> Iterator["Page"]:
        from playwright.sync_api import sync_playwright
        user_data_dir = _resolve_user_data_dir()
        profile_dir = _resolve_profile_directory()
        headless = _resolve_headless()
        use_persistent = _should_use_persistent_context(user_data_dir)
        using_default_user_data_dir = _is_default_user_data_dir(user_data_dir)

        if use_persistent:
            if using_default_user_data_dir:
                if not kill_running_edge():
                    raise RuntimeError(
                        "Unable to close existing Edge processes required for persistent profile launch. "
                        "Close all Edge windows (including background/profile windows) and retry."
                    )
            else:
                Path(user_data_dir).mkdir(parents=True, exist_ok=True)
                log.info(
                    "Using dedicated persistent Edge user-data-dir: %s",
                    user_data_dir,
                )
        else:
            log.warning(
                "Default Edge user-data-dir detected; using non-persistent launch mode "
                "(set PLAYWRIGHT_EDGE_USER_DATA_DIR to a non-default dir or COMPASS_GO_FORCE_PERSISTENT=1 to force)"
            )

        log.info("Launching Edge profile: %s\\%s (headless=%s)", user_data_dir, profile_dir, headless)

        with sync_playwright() as pw:
            browser = None
            context = None
            last_exc: Exception | None = None
            for attempt in range(1, LAUNCH_RETRY_ATTEMPTS + 1):
                try:
                    if use_persistent:
                        log.info("Attempt %d: Launching persistent context with profile", attempt)
                        context = pw.chromium.launch_persistent_context(
                            user_data_dir=user_data_dir,
                            channel="msedge",
                            headless=headless,
                            args=[f"--profile-directory={profile_dir}"],
                        )
                        browser = context.browser
                        if browser is None:
                            raise RuntimeError("Persistent context did not return a browser handle")
                    else:
                        log.info("Attempt %d: Launching browser (non-persistent mode)", attempt)
                        browser = pw.chromium.launch(
                            channel="msedge",
                            headless=headless,
                            args=[f"--profile-directory={profile_dir}"],
                        )
                        context = browser.new_context()
                    self._wire_lifecycle_logging(browser, context)
                    if use_persistent:
                        log.info("Attempt %d: Persistent context created successfully", attempt)
                    else:
                        log.info("Attempt %d: Context created successfully", attempt)
                    break
                except Exception as exc:  # noqa: BLE001 - launch can fail for many runtime reasons
                    last_exc = exc
                    log.error("Attempt %d: Browser launch failed: %s", attempt, exc, exc_info=True)
                    if context:
                        try:
                            context.close()
                        except Exception as close_exc:
                            log.warning("Failed to close context: %s", close_exc)
                    if browser:
                        try:
                            browser.close()
                        except Exception as close_exc:
                            log.warning("Failed to close browser: %s", close_exc)
                    if attempt >= LAUNCH_RETRY_ATTEMPTS:
                        raise RuntimeError(
                            "Unable to launch Edge browser after retries. "
                            "Close all Edge windows manually and retry."
                        ) from exc
                    log.warning(
                        "Edge browser launch failed (attempt %d/%d): %s",
                        attempt,
                        LAUNCH_RETRY_ATTEMPTS,
                        exc,
                    )
                    if use_persistent:
                        kill_running_edge()

            if context is None or browser is None:
                # Defensive fallback (should never execute due raise above).
                raise RuntimeError("Unable to create Edge browser context") from last_exc
            try:
                # Keep one inert page open so an SSO-driven window.close on the
                # active auth tab does not collapse the entire browser process.
                keepalive_page = context.new_page()
                try:
                    keepalive_page.goto("about:blank", wait_until="domcontentloaded")
                except Exception:
                    # about:blank is typically already loaded; ignore navigation noise.
                    pass
                log.info("Created keepalive page to prevent single-tab browser shutdown")

                log.info("Creating new page from context")
                page = context.new_page()
                log.info("Navigating to entry URL: %s", self._entry_url)
                page.goto(self._entry_url, wait_until="domcontentloaded")
                log.info("Navigation successful, yielding page")
                yield page
            except Exception as nav_exc:
                log.error("Failed during page creation or navigation: %s", nav_exc, exc_info=True)
                raise
            finally:
                log.info("Closing context and browser")
                try:
                    context.close()
                except Exception as ctx_exc:
                    log.warning("Failed to close context: %s", ctx_exc)
                try:
                    browser.close()
                except Exception as browser_exc:
                    log.warning("Failed to close browser: %s", browser_exc)


