"""Auth flow — get from entry URL to a ready ScanPage.

Race-pair detection: poll a small set of readiness anchors and take the first
match. Idempotent — safe to call at any point if we're unsure of the state.

Possible landing states (in expected order):
  1. "Welcome back!" confirm page         -> click Continue
  2. "Now, choose your location:" picker  -> click Finish Setup
  3. Already on Scan / app shell          -> return immediately
"""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from .diagnostics import capture_failure, setup_file_logging
from .pages.location_picker_page import LocationPickerPage
from .pages.login_confirm_page import LoginConfirmPage
from .pages.scan_page import ScanPage

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

MAX_AUTH_SECONDS = int(os.getenv("COMPASS_GO_AUTH_TIMEOUT_S", "60"))
POLL_INTERVAL_S = 0.5


class AuthFlow:
    """Composes the three possible landing pages into a single 'reach Scan'."""

    def __init__(self, page: "Page"):
        setup_file_logging()
        self._page = page
        self._login = LoginConfirmPage(page)
        self._location = LocationPickerPage(page)
        self._scan = ScanPage(page)

    def ensure_signed_in(self) -> ScanPage:
        """Block until ScanPage is reachable. Raise if it doesn't appear."""
        deadline = time.monotonic() + MAX_AUTH_SECONDS
        last_state = "unknown"

        while time.monotonic() < deadline:
            if self._scan.is_displayed():
                log.info("Auth: ScanPage ready (state=%s)", last_state)
                return self._scan

            if self._login.is_displayed():
                last_state = "login_confirm"
                log.info("Auth: clicking Continue on Welcome back page")
                self._login.continue_as_current_user()
                time.sleep(POLL_INTERVAL_S)
                continue

            if self._location.is_displayed():
                last_state = "location_picker"
                log.info("Auth: clicking Finish Setup on Location picker")
                self._location.finish_setup()
                time.sleep(POLL_INTERVAL_S)
                continue

            last_state = "waiting"
            time.sleep(POLL_INTERVAL_S)

        capture_failure(self._page, f"auth_timeout_{last_state}")
        raise TimeoutError(
            f"AuthFlow: never reached ScanPage within {MAX_AUTH_SECONDS}s "
            f"(last_state={last_state})"
        )
