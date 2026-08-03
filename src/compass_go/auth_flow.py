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

DEFAULT_ENTRY_URL = "https://go.avisbudget.palantirfoundry.com/"
MAX_AUTH_SECONDS = int(os.getenv("COMPASS_GO_AUTH_TIMEOUT_S", "300"))
POLL_INTERVAL_S = 0.5


class AuthFlow:
    """Composes the three possible landing pages into a single 'reach Scan'."""

    def __init__(self, page: "Page"):
        setup_file_logging()
        self._page = page
        self._entry_url = os.getenv("COMPASS_GO_ENTRY_URL", DEFAULT_ENTRY_URL)
        self._sync_pages()

    def _sync_pages(self) -> None:
        self._login = LoginConfirmPage(self._page)
        self._location = LocationPickerPage(self._page)
        self._scan = ScanPage(self._page)

    @staticmethod
    def _page_priority(url: str) -> int:
        u = (url or "").strip().lower()
        if not u or u == "about:blank":
            return -2
        score = 0
        if "/scan/" in u:
            score += 4
        if "go.avisbudget.palantirfoundry.com" in u:
            score += 3
        if "avisbudget.palantirfoundry.com" in u:
            score += 2
        if "/multipass/" in u:
            score += 1
        return score

    def _adopt_best_context_page(self) -> None:
        try:
            pages = [p for p in self._page.context.pages if not p.is_closed()]
        except Exception:
            return

        if not pages:
            return

        current = None if self._page.is_closed() else self._page
        best = max(pages, key=lambda p: self._page_priority(p.url or ""))
        if current is best:
            return

        best_score = self._page_priority(best.url or "")
        current_score = self._page_priority((current.url or "") if current is not None else "")
        if best_score <= current_score:
            return

        self._page = best
        self._sync_pages()
        log.info("Auth: switched to better context page (url=%s)", best.url)

    def _ensure_page_alive(self) -> None:
        if not self._page.is_closed():
            return

        candidates = []
        context = None
        try:
            context = self._page.context
            candidates = list(self._page.context.pages)
        except Exception:
            candidates = []

        for candidate in reversed(candidates):
            try:
                if not candidate.is_closed():
                    self._page = candidate
                    if (candidate.url or "").strip() in {"", "about:blank"}:
                        candidate.goto(self._entry_url, wait_until="domcontentloaded")
                    self._sync_pages()
                    log.warning(
                        "Auth: original page closed; switched to another open page in context"
                    )
                    return
            except Exception:
                continue

        if context is not None:
            try:
                replacement = context.new_page()
                replacement.goto(self._entry_url, wait_until="domcontentloaded")
                self._page = replacement
                self._sync_pages()
                log.warning(
                    "Auth: original page closed; created replacement page and re-navigated"
                )
                return
            except Exception:
                log.exception(
                    "Auth: failed to recreate page after close (entry_url=%s)",
                    self._entry_url,
                )

        snapshot = []
        for candidate in candidates:
            try:
                snapshot.append({"url": candidate.url, "closed": candidate.is_closed()})
            except Exception:
                snapshot.append({"url": "<unavailable>", "closed": "<error>"})
        log.error(
            "Auth: no usable page after close (entry_url=%s, page_count=%d, pages=%s)",
            self._entry_url,
            len(candidates),
            snapshot,
        )

        raise RuntimeError(
            "AuthFlow: browser page closed during sign-in before ScanPage was reachable"
        )

    def ensure_signed_in(self) -> ScanPage:
        """Block until ScanPage is reachable. Raise if it doesn't appear."""
        deadline = time.monotonic() + MAX_AUTH_SECONDS
        last_state = "unknown"

        while time.monotonic() < deadline:
            self._adopt_best_context_page()
            self._ensure_page_alive()
            self._adopt_best_context_page()

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
