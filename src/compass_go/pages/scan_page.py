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
VEHICLE_DETAILS_HEADING = "Vehicle Details"

OUTCOME_TIMEOUT_S_DEFAULT = 45
OUTCOME_POLL_INTERVAL_S = 1.0


def _resolve_outcome_timeout_s() -> int:
    raw = os.getenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "").strip()
    if not raw:
        return OUTCOME_TIMEOUT_S_DEFAULT
    try:
        val = int(raw)
        return val if val > 0 else OUTCOME_TIMEOUT_S_DEFAULT
    except ValueError:
        return OUTCOME_TIMEOUT_S_DEFAULT


def _resolve_input_timeout_s() -> int:
    raw = os.getenv("COMPASS_GO_INPUT_TIMEOUT_S", "").strip()
    if not raw:
        return _resolve_outcome_timeout_s()
    try:
        val = int(raw)
        return val if val > 0 else _resolve_outcome_timeout_s()
    except ValueError:
        return _resolve_outcome_timeout_s()


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

        # A persistent Edge profile may reopen into a stale Vehicle Details
        # state (plain details or a Vehicle Not Found card). In either case
        # the MVA input is absent until the back arrow is clicked.
        self._dismiss_stale_pre_submit_state()

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
        input_wait_s = _resolve_input_timeout_s()
        log.info(
            "ScanPage.submit: waiting for MVA/VIN input (timeout=%ds, poll=1.0s)",
            input_wait_s,
        )
        self._wait_for_input_visible(input_locator, timeout_s=input_wait_s)
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

    def _wait_for_input_visible(self, input_locator, timeout_s: int) -> None:
        """Wait for the MVA/VIN input using explicit polling.

        Compass GO can render this field slowly on some sessions. Polling
        every second avoids a single fixed wait attempt while keeping log
        timing predictable for diagnostics.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if input_locator.is_visible():
                    return
            except Exception:
                # Page can be in transient re-render; continue polling.
                pass
            time.sleep(OUTCOME_POLL_INTERVAL_S)

        raise TimeoutError(
            "ScanPage.submit: MVA/VIN input did not become visible "
            f"within {timeout_s}s"
        )

    def _is_mva_input_visible(self) -> bool:
        try:
            return self._page.get_by_label(INPUT_ARIA_LABEL).first.is_visible()
        except Exception:
            return False

    def _dismiss_stale_pre_submit_state(self) -> None:
        """Dismiss stale Vehicle Details states before trying to fill MVA.

        Handles two stale-entry cases:
        1. Vehicle Not Found card still visible from prior run.
        2. Plain Vehicle Details view still visible from prior run.
        """
        if self._is_mva_input_visible():
            return

        try:
            not_found_visible = self._page.get_by_text(NOT_FOUND_TEXT, exact=True).first.is_visible()
        except Exception:
            not_found_visible = False

        try:
            details_visible = self._page.get_by_role(
                "heading", name=VEHICLE_DETAILS_HEADING
            ).first.is_visible()
        except Exception:
            details_visible = False

        if not not_found_visible and not details_visible:
            return

        reason = "Vehicle Not Found" if not_found_visible else "Vehicle Details"
        log.info("ScanPage.submit: stale '%s' state present — clicking back arrow", reason)
        try:
            self._page.locator(BACK_BUTTON_SELECTOR).first.click(timeout=2_000)
        except Exception:
            log.exception("ScanPage.submit: back-arrow click failed on stale state")
