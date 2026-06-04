"""'Now, choose your location' picker.

TODO: confirm Finish Setup button + select dropdown selectors from DOM.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


class LocationPickerPage:
    def __init__(self, page: "Page"):
        self._page = page

    def is_displayed(self) -> bool:
        return self._page.get_by_role(
            "heading", name="Now, choose your location:"
        ).is_visible()

    def finish_setup(self) -> None:
        self._page.get_by_role("button", name="Finish Setup").click()
