"""Scan Vehicle view — bottom-nav 'Scan' tab containing the MVA/VIN input
+ Enter button. A 'Begin Scanning' button appears in some first-run states
and is clicked conditionally if present.
"""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from ..outcomes import (
    DEFAULT_DETECTORS,
    MVANotFoundError,
    Outcome,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..outcomes import Detector
    from .vehicle_details_page import VehicleDetailsPage

log = logging.getLogger(__name__)

CONTAINER = ".enter-mva-vin"
INPUT_ARIA_LABEL = "Or enter MVA/VIN"
BEGIN_SCANNING_NAME = "Begin Scanning"
SCAN_TAB_SELECTOR = 'button[role="tab"][data-key="scan"]'
BACK_BUTTON_SELECTOR = "button.back-button"
NOT_FOUND_TEXT = "Vehicle Not Found"

OUTCOME_TIMEOUT_S_DEFAULT = 20
OUTCOME_POLL_INTERVAL_S = 0.5


def _resolve_outcome_timeout_s() -> int:
    raw = os.getenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "").strip()
    if not raw:
        return OUTCOME_TIMEOUT_S_DEFAULT
    try:
        val = int(raw)
        return val if val > 0 else OUTCOME_TIMEOUT_S_DEFAULT
    except ValueError:
        return OUTCOME_TIMEOUT_S_DEFAULT


class ScanPage:
    def __init__(self, page: "Page"):
        self._page = page

    @property
    def page(self) -> "Page":
        """Public accessor for the underlying Playwright Page (diagnostics)."""
        return self._page

    def _scan_nav(self):
        # Bottom-nav "Scan" tab — present on every authenticated view.
        candidates = [
            self._page.locator(SCAN_TAB_SELECTOR).first,
            self._page.get_by_role("tab", name="Scan", exact=True).first,
            self._page.get_by_role("button", name="Scan", exact=True).first,
        ]
        for c in candidates:
            try:
                if c.is_visible():
                    return c
            except Exception:
                continue
        return None

    def is_displayed(self) -> bool:
        # Ready as soon as the bottom-nav Scan tab, the Begin Scanning button,
        # or the MVA/VIN input is visible — these are the three Scan-related
        # surfaces the auth race may land on.
        if self._scan_nav() is not None:
            return True
        if self._page.get_by_role("button", name=BEGIN_SCANNING_NAME).is_visible():
            return True
        return self._page.get_by_label(INPUT_ARIA_LABEL).first.is_visible()

    def submit(self, mva: str) -> "VehicleDetailsPage":
        from .vehicle_details_page import VehicleDetailsPage

        # A stale 'Vehicle Not Found' card from a prior session can be
        # the entry state when the Edge profile preserves the previous
        # page. The MVA input doesn't render until the card is dismissed,
        # which requires the back arrow (clicking bottom-nav Scan does
        # not reset it). Best-effort: try the back arrow first.
        self._dismiss_stale_not_found_card()

        # If the app landed on a non-Scan tab (e.g. Off Lot), click the
        # bottom-nav Scan first so the MVA input can render.
        nav = self._scan_nav()
        if nav is not None and nav.get_attribute("aria-selected") != "true":
            log.info("ScanPage.submit: clicking bottom-nav Scan")
            try:
                nav.click()
            except Exception:
                log.exception("ScanPage.submit: bottom-nav Scan click failed")
        else:
            log.info("ScanPage.submit: bottom-nav Scan already active or absent")

        # Begin Scanning only appears in some first-run states. Check briefly;
        # if it's not visible within 2s, assume we're already past it.
        begin = self._page.get_by_role("button", name=BEGIN_SCANNING_NAME)
        try:
            begin.wait_for(state="visible", timeout=2_000)
            log.info("ScanPage.submit: Begin Scanning visible — clicking")
            begin.click()
        except Exception:
            log.info("ScanPage.submit: Begin Scanning not present — skipping")

        container = self._page.locator(CONTAINER)
        input_locator = self._page.get_by_label(INPUT_ARIA_LABEL).first
        if container.count():
            # Prefer the legacy scoped container when present (test fixture).
            input_locator = container.get_by_label(INPUT_ARIA_LABEL).first
        input_wait_ms = _resolve_outcome_timeout_s() * 1000
        log.info("ScanPage.submit: waiting for MVA/VIN input (timeout=%dms)", input_wait_ms)
        input_locator.wait_for(timeout=input_wait_ms)
        log.info("ScanPage.submit: filling MVA=%s", mva)
        input_locator.fill(mva)

        # Enter button sits next to the input. Use role+name; if multiple
        # Enter buttons exist, the one adjacent to the focused input wins.
        log.info("ScanPage.submit: clicking Enter")
        self._page.get_by_role("button", name="Enter", exact=True).first.click()

        outcome = self._await_submit_outcome()
        if outcome is Outcome.MVA_NOT_FOUND:
            log.warning("ScanPage.submit: MVA %s — Vehicle Not Found", mva)
            raise MVANotFoundError(mva)
        if outcome is not Outcome.DETAILS_READY:
            # Defensive: a new detector was added to DEFAULT_DETECTORS but
            # this method wasn't updated. Fail fast rather than silently
            # returning a stale VehicleDetailsPage.
            raise RuntimeError(
                f"ScanPage.submit: unhandled outcome {outcome!r} for MVA {mva}"
            )
        log.info("ScanPage.submit: Vehicle Details ready")
        return VehicleDetailsPage(self._page)

    def _await_submit_outcome(
        self,
        detectors: "tuple[Detector, ...]" = DEFAULT_DETECTORS,
        timeout_s: int | None = None,
    ) -> Outcome:
        """Poll the post-submit page for any known terminal outcome.

        Raises TimeoutError if no detector fires within the timeout window.
        """
        timeout_s = timeout_s if timeout_s is not None else _resolve_outcome_timeout_s()
        log.info("ScanPage.submit: waiting for outcome (timeout=%ds)", timeout_s)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for detector in detectors:
                outcome = detector(self._page)
                if outcome is not None:
                    return outcome
            time.sleep(OUTCOME_POLL_INTERVAL_S)
        raise TimeoutError(
            f"No known outcome detected within {timeout_s}s after MVA submit"
        )

    def _dismiss_stale_not_found_card(self) -> None:
        """Click the back arrow if a 'Vehicle Not Found' card is on screen.

        Handles the case where the persistent Edge profile lands on the
        previous session's error page. Best-effort: if no card is visible
        or the click fails, log and continue — the normal nav flow then
        runs and will surface any real problem via the input timeout.
        """
        try:
            card = self._page.get_by_text(NOT_FOUND_TEXT, exact=True).first
            if not card.is_visible():
                return
        except Exception:
            return
        log.info("ScanPage.submit: stale 'Vehicle Not Found' card present — clicking back arrow")
        try:
            self._page.locator(BACK_BUTTON_SELECTOR).first.click()
        except Exception:
            log.exception("ScanPage.submit: back-arrow click failed on stale card")
