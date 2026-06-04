"""Scan Vehicle view — bottom-nav 'Scan' tab containing the MVA/VIN input
+ Enter button. A 'Begin Scanning' button appears in some first-run states
and is clicked conditionally if present.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .vehicle_details_page import VehicleDetailsPage

log = logging.getLogger(__name__)

CONTAINER = ".enter-mva-vin"
INPUT_ARIA_LABEL = "Or enter MVA/VIN"
BEGIN_SCANNING_NAME = "Begin Scanning"
SCAN_TAB_SELECTOR = 'button[role="tab"][data-key="scan"]'


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
        log.info("ScanPage.submit: waiting for MVA/VIN input")
        input_locator.wait_for(timeout=90_000)
        log.info("ScanPage.submit: filling MVA=%s", mva)
        input_locator.fill(mva)

        # Enter button sits next to the input. Use role+name; if multiple
        # Enter buttons exist, the one adjacent to the focused input wins.
        log.info("ScanPage.submit: clicking Enter")
        self._page.get_by_role("button", name="Enter", exact=True).first.click()
        log.info("ScanPage.submit: waiting for Vehicle Details heading")
        self._page.get_by_role("heading", name="Vehicle Details").wait_for(timeout=90_000)
        log.info("ScanPage.submit: Vehicle Details ready")
        return VehicleDetailsPage(self._page)
