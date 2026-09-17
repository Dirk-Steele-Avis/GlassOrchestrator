from unittest.mock import MagicMock

import pytest

from src.compass_go.pages.login_confirm_page import (
    HEADING_TEXT,
    WWID,
    WWID_INPUT_SELECTOR,
    LoginConfirmPage,
)


def test_is_displayed_uses_exact_confirm_user_heading():
    page = MagicMock()
    page.get_by_role.return_value.is_visible.return_value = True
    page.locator.return_value.count.return_value = 1

    assert LoginConfirmPage(page).is_displayed()

    page.get_by_role.assert_called_once_with(
        "heading", name=HEADING_TEXT, exact=True
    )
    page.locator.assert_called_once_with(WWID_INPUT_SELECTOR)


def test_is_displayed_rejects_heading_without_wwid_input():
    page = MagicMock()
    page.get_by_role.return_value.is_visible.return_value = True
    page.locator.return_value.count.return_value = 0

    assert not LoginConfirmPage(page).is_displayed()


def test_continue_as_current_user_fills_fixed_wwid_once():
    page = MagicMock()
    wwid_input = page.locator.return_value.first
    wwid_input.input_value.return_value = WWID

    LoginConfirmPage(page).continue_as_current_user()

    page.locator.assert_called_once_with(WWID_INPUT_SELECTOR)
    wwid_input.wait_for.assert_called_once_with(state="attached", timeout=10_000)
    wwid_input.fill.assert_called_once_with("764567")
    page.keyboard.type.assert_not_called()
    page.keyboard.press.assert_not_called()


def test_continue_as_current_user_raises_when_input_is_missing():
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = TimeoutError("missing")
    page.get_by_role.return_value.is_visible.return_value = True

    with pytest.raises(TimeoutError, match="missing"):
        LoginConfirmPage(page).continue_as_current_user()


def test_continue_as_current_user_raises_when_value_does_not_match():
    page = MagicMock()
    page.locator.return_value.first.input_value.return_value = ""
    page.get_by_role.return_value.is_visible.return_value = True

    with pytest.raises(RuntimeError, match="did not retain"):
        LoginConfirmPage(page).continue_as_current_user()


def test_continue_as_current_user_accepts_dialog_auto_advance():
    page = MagicMock()
    wwid_input = page.locator.return_value.first
    wwid_input.input_value.side_effect = TimeoutError("detached")
    page.get_by_role.return_value.is_visible.return_value = False

    LoginConfirmPage(page).continue_as_current_user()

    wwid_input.fill.assert_called_once_with(WWID)