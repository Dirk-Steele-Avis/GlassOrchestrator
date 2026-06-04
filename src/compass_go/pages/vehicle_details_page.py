"""Vehicle Details view (post-scan).

Locator strategy: stable `data-key` attributes on each row. React-Aria
generated ids are intentionally avoided. See
`Docs/compassgo-refactor-plan.md` for the broader scraper plan.

Confirmed data-key values:
    vinNo          -> VIN row
    mvaNo          -> MVA row
    makeModelDesc  -> Description row (Make/Model)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

DATA_KEY_VIN = "vinNo"
DATA_KEY_MVA = "mvaNo"
DATA_KEY_DESC = "makeModelDesc"

# Each row renders the value in a separate <td role="gridcell"> whose
# `data-key` ends in ".1" (".0" is the rowheader/label cell). The cell is
# initially populated with a skeleton loader and only gets text once data
# loads — so we must wait for non-empty text rather than reading immediately.
VALUE_CELL_TIMEOUT_MS = 60_000


class VehicleDetailsPage:
    """Read-only view: scraped fields + expand/collapse + back navigation."""

    def __init__(self, page: "Page"):
        self._page = page

    def read(self, data_key: str) -> str:
        """Return the value cell text for the row with the given data-key.

        Waits up to VALUE_CELL_TIMEOUT_MS for the cell to render non-empty
        text (skeleton loader resolved). Returns "" if the wait times out
        or the row doesn't exist — caller (writer) coerces to N/A.
        """
        value_cell = self._page.locator(
            f'td[data-key="{data_key}.1"][role="gridcell"]'
        ).first
        try:
            value_cell.wait_for(state="visible", timeout=VALUE_CELL_TIMEOUT_MS)
            # Poll for non-empty text — skeleton loaders render an empty div
            # until data arrives.
            self._page.wait_for_function(
                """([sel]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const txt = (el.innerText || '').trim();
                    return txt.length > 0;
                }""",
                arg=[f'td[data-key="{data_key}.1"][role="gridcell"]'],
                timeout=VALUE_CELL_TIMEOUT_MS,
            )
            return value_cell.inner_text().strip()
        except Exception:
            log.warning("read(%s): value cell empty after %dms",
                        data_key, VALUE_CELL_TIMEOUT_MS)
            return ""

    def expand_show_more(self) -> None:
        """Click Show More to reveal hidden rows (including VIN).

        Idempotent: if already expanded (Show Less visible), this is a no-op.
        Button has no stable id/data-attr; matched by accessible name.
        """
        show_less = self._page.get_by_role("button", name="Show Less", exact=True)
        if show_less.count() > 0 and show_less.first.is_visible():
            return
        show_more = self._page.get_by_role("button", name="Show More", exact=True)
        if show_more.count() == 0 or not show_more.first.is_visible():
            log.info("expand_show_more: no Show More button — skipping")
            return
        show_more.first.click()
        try:
            show_less.first.wait_for(timeout=5_000)
        except Exception:
            log.info("expand_show_more: Show Less did not appear — continuing")

    def back(self) -> None:
        """Navigate back via the chevron-left button (class `back-button`)."""
        self._page.locator("button.back-button").click()
