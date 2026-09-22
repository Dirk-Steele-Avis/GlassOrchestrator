import asyncio
from unittest.mock import AsyncMock, MagicMock


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