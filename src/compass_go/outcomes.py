"""Post-submit outcome detection for the Compass GO Scan view.

After submitting an MVA, the app can settle on one of several outcomes:
- Vehicle Details heading (the happy path)
- "Vehicle Not Found" error card
- (future) other terminal states

`ScanPage.submit` polls these detectors in order until one fires or the
outcome timeout elapses. Each detector is a small callable bound to the
Playwright Page that returns an `Outcome` enum value (or None if it can't
yet make a determination).

To add a new detector:
    1. Define a function `def detect_<name>(page) -> Outcome | None`.
    2. Append it to `DEFAULT_DETECTORS` below in the desired priority order.
    3. If the new outcome should bubble up as its own exception, extend
       `ScanPage.submit` to map it accordingly.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from playwright.sync_api import Page


class Outcome(str, Enum):
    DETAILS_READY = "details_ready"
    MVA_NOT_FOUND = "mva_not_found"


class MVANotFoundError(Exception):
    """Raised by ScanPage.submit when Compass GO reports 'Vehicle Not Found'."""


# Selectors / accessible-name fragments for the "Vehicle Not Found" card.
NOT_FOUND_HEADING_TEXT = "Vehicle Not Found"
DETAILS_HEADING_TEXT = "Vehicle Details"
DETAILS_MVA_CELL_SELECTOR = 'td[data-key="mvaNo.1"][role="gridcell"]'


def detect_mva_not_found(page: "Page") -> Outcome | None:
    """Return MVA_NOT_FOUND if Compass GO shows an invalid-MVA response."""
    try:
        locator = page.get_by_text(NOT_FOUND_HEADING_TEXT, exact=True).first
        if locator.is_visible():
            return Outcome.MVA_NOT_FOUND
    except Exception:
        pass
    return None


def detect_details_ready(page: "Page") -> Outcome | None:
    """Return DETAILS_READY once the details shell contains loaded MVA data."""
    try:
        heading = page.get_by_role("heading", name=DETAILS_HEADING_TEXT).first
        mva_cell = page.locator(DETAILS_MVA_CELL_SELECTOR).first
        if heading.is_visible() and mva_cell.is_visible() and mva_cell.inner_text().strip():
            return Outcome.DETAILS_READY
    except Exception:
        pass
    return None


Detector = Callable[["Page"], "Outcome | None"]

# Order matters: error detectors first so a terminal failure short-circuits
# the happy path. Add new detectors at the appropriate priority.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_mva_not_found,
    detect_details_ready,
)
