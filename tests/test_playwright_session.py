import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_microsoft_auth_wait_holds_until_approval_completes():
    from playwright_prototype import session

    page = MagicMock()
    page.url = "https://login.microsoftonline.com/common/oauth2/authorize"
    page.goto = AsyncMock()

    async def approve(_ms):
        page.url = "https://avisbudget.palantirfoundry.com/workspace"

    page.wait_for_timeout = AsyncMock(side_effect=approve)

    asyncio.run(session._wait_for_microsoft_auth_completion(page))

    page.wait_for_timeout.assert_awaited_once_with(session.MICROSOFT_AUTH_POLL_INTERVAL_MS)
    page.goto.assert_not_awaited()


def test_microsoft_auth_wait_timeout_raises_precise_error(monkeypatch):
    from playwright_prototype import session

    page = MagicMock()
    page.url = "https://login.microsoftonline.com/common/oauth2/authorize"
    page.wait_for_timeout = AsyncMock()
    monkeypatch.setattr(session, "MICROSOFT_AUTH_COMPLETION_TIMEOUT_MS", 0)

    with pytest.raises(RuntimeError, match="Microsoft authentication did not complete"):
        asyncio.run(session._wait_for_microsoft_auth_completion(page))

    page.wait_for_timeout.assert_not_awaited()


def test_advance_waits_for_mfa_instead_of_navigating_away(monkeypatch):
    from playwright_prototype import session

    page = MagicMock()
    page.url = session.LOGIN_URL
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()

    async def select_account(_page):
        page.url = "https://login.microsoftonline.com/common/oauth2/authorize"

    async def complete_auth(_page):
        page.url = session.LOGIN_URL

    auth_wait = AsyncMock(side_effect=complete_auth)

    monkeypatch.setattr(session, "_allow_compass_home_consent", AsyncMock(return_value=False))
    monkeypatch.setattr(session, "_is_on_workshop_home", AsyncMock(return_value=False))
    monkeypatch.setattr(session, "_is_on_login_page", AsyncMock(return_value=False))
    monkeypatch.setattr(session, "_is_on_sso_picker", AsyncMock(return_value=True))
    monkeypatch.setattr(session, "_select_first_sso_account", AsyncMock(side_effect=select_account))
    monkeypatch.setattr(session, "_wait_for_microsoft_auth_completion", auth_wait)
    monkeypatch.setattr(session, "_is_on_compass_app_page", AsyncMock(return_value=True))
    monkeypatch.setattr(session, "_is_on_wwid_screen", AsyncMock(return_value=False))

    result = asyncio.run(session._advance_existing_session_page(page))

    assert result is page
    auth_wait.assert_awaited_once_with(page)
    page.goto.assert_not_awaited()


def test_exact_compass_home_consent_is_allowed():
    from playwright_prototype import session

    page = MagicMock()
    consent_text = MagicMock()
    consent_text.count = AsyncMock(return_value=1)
    read_scope = MagicMock()
    read_scope.count = AsyncMock(return_value=1)
    write_scope = MagicMock()
    write_scope.count = AsyncMock(return_value=1)
    page.get_by_text.side_effect = [consent_text, read_scope, write_scope]
    allow_button = MagicMock()
    allow_button.count = AsyncMock(return_value=1)
    allow_button.click = AsyncMock()
    page.get_by_role.return_value = allow_button
    mva_input = MagicMock()
    mva_input.count = AsyncMock(return_value=1)
    mva_input.first.wait_for = AsyncMock()
    page.locator.return_value = mva_input

    result = asyncio.run(session._allow_compass_home_consent(page))

    assert result is True
    page.get_by_role.assert_called_once_with("button", name="Allow", exact=True)
    allow_button.click.assert_awaited_once_with(timeout=8_000)
    mva_input.first.wait_for.assert_awaited_once_with(state="visible", timeout=20_000)


def test_absent_compass_home_consent_does_not_click_allow():
    from playwright_prototype import session

    page = MagicMock()
    consent_text = MagicMock()
    consent_text.count = AsyncMock(return_value=0)
    page.get_by_text.return_value = consent_text

    result = asyncio.run(session._allow_compass_home_consent(page))

    assert result is False
    page.get_by_role.assert_not_called()


def test_workshop_home_skips_slow_session_probes(monkeypatch):
    from playwright_prototype import session

    page = MagicMock()
    page.url = session.LOGIN_URL
    page.goto = AsyncMock()

    consent_probe = AsyncMock(return_value=False)
    workshop_ready = AsyncMock(return_value=True)
    login_probe = AsyncMock()
    sso_probe = AsyncMock()
    app_probe = AsyncMock()
    picker_probe = AsyncMock()
    wwid_probe = AsyncMock()

    monkeypatch.setattr(session, "_allow_compass_home_consent", consent_probe)
    monkeypatch.setattr(session, "_is_on_workshop_home", workshop_ready)
    monkeypatch.setattr(session, "_is_on_login_page", login_probe)
    monkeypatch.setattr(session, "_is_on_sso_picker", sso_probe)
    monkeypatch.setattr(session, "_is_on_compass_app_page", app_probe)
    monkeypatch.setattr(session, "_is_on_compass_mobile_picker", picker_probe)
    monkeypatch.setattr(session, "_is_on_wwid_screen", wwid_probe)

    result = asyncio.run(session._advance_existing_session_page(page))

    assert result is page
    consent_probe.assert_awaited_once_with(page)
    workshop_ready.assert_awaited_once_with(page)
    page.goto.assert_not_awaited()
    login_probe.assert_not_awaited()
    sso_probe.assert_not_awaited()
    app_probe.assert_not_awaited()
    picker_probe.assert_not_awaited()
    wwid_probe.assert_not_awaited()