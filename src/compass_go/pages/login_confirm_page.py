"""'Confirm User' WWID dialog.

The dialog renders a segmented OTP input (input-otp library). The backing
hidden input has `autocomplete="one-time-code"` — filling it propagates to
all segments. The dialog auto-submits when the WWID length matches; we
also press Enter as a fallback in case auto-submit is disabled.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

HEADING_TEXT = "Confirm User"
WWID_INPUT_SELECTOR = 'input[autocomplete="one-time-code"]'


class LoginConfirmPage:
    def __init__(self, page: "Page"):
        self._page = page

    def is_displayed(self) -> bool:
        try:
            return self._page.get_by_role(
                "heading", name=HEADING_TEXT, exact=True
            ).is_visible()
        except Exception:
            return False

    def continue_as_current_user(self) -> None:
        wwid = os.getenv("GLASS_LOGIN_ID", "").strip()
        if not wwid:
            try:
                from config.config_loader import get_config
                wwid = (get_config("login_id") or "").strip()
            except Exception:
                log.exception("LoginConfirm: config fallback for login_id failed")
        if not wwid:
            log.warning(
                "LoginConfirm: Confirm User dialog shown but no WWID available "
                "(set GLASS_LOGIN_ID or config 'login_id')"
            )
            return

        wwid_input = self._page.locator(WWID_INPUT_SELECTOR).first
        try:
            wwid_input.wait_for(state="attached", timeout=10_000)
        except Exception:
            log.exception("LoginConfirm: WWID input not found")
            return

        log.info("LoginConfirm: filling WWID (len=%d)", len(wwid))
        try:
            wwid_input.fill(wwid)
        except Exception:
            # Fallback for hidden/synthetic inputs: focus + type.
            log.info("LoginConfirm: .fill failed, falling back to focus+type")
            try:
                wwid_input.focus()
            except Exception:
                pass
            self._page.keyboard.type(wwid, delay=30)

        # Some OTP dialogs auto-submit on full length; others require Enter.
        try:
            self._page.keyboard.press("Enter")
        except Exception:
            pass
