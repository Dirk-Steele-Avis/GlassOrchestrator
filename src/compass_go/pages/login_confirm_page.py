"""'Confirm User' WWID dialog.

The dialog renders a segmented OTP input (input-otp library). The backing
hidden input has `autocomplete="one-time-code"`; filling it propagates to
all segments and the dialog auto-submits when the WWID length matches.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

HEADING_TEXT = "Confirm User"
WWID_INPUT_SELECTOR = 'input[autocomplete="one-time-code"]'
WWID = "764567"


class LoginConfirmPage:
    def __init__(self, page: "Page"):
        self._page = page

    def _heading_is_visible(self) -> bool:
        return self._page.get_by_role(
            "heading", name=HEADING_TEXT, exact=True
        ).is_visible()

    def is_displayed(self) -> bool:
        try:
            return (
                self._heading_is_visible()
                and self._page.locator(WWID_INPUT_SELECTOR).count() == 1
            )
        except Exception:
            return False

    def continue_as_current_user(self) -> None:
        wwid_input = self._page.locator(WWID_INPUT_SELECTOR).first
        wwid_input.wait_for(state="attached", timeout=10_000)

        log.info("LoginConfirm: filling fixed WWID")
        wwid_input.fill(WWID)
        self._page.get_by_role(
            "heading", name=HEADING_TEXT, exact=True
        ).wait_for(state="hidden", timeout=10_000)
        log.info("LoginConfirm: dialog cleared after WWID entry")
