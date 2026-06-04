"""Persistent bottom-tab navigation (Scan / On Lot / Off Lot).

TODO: confirm tab role + selected-state attribute from DOM snippet.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


class BottomNav:
    def __init__(self, page: "Page"):
        self._page = page

    def goto_scan(self) -> None:
        self._page.get_by_role("button", name="Scan").click()
