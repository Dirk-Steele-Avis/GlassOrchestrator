import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_workshop_home_skips_slow_session_probes(monkeypatch):
    from playwright_prototype import session

    page = MagicMock()
    page.url = session.LOGIN_URL
    page.goto = AsyncMock()

    workshop_ready = AsyncMock(return_value=True)
    login_probe = AsyncMock()
    sso_probe = AsyncMock()
    app_probe = AsyncMock()
    picker_probe = AsyncMock()
    wwid_probe = AsyncMock()

    monkeypatch.setattr(session, "_is_on_workshop_home", workshop_ready)
    monkeypatch.setattr(session, "_is_on_login_page", login_probe)
    monkeypatch.setattr(session, "_is_on_sso_picker", sso_probe)
    monkeypatch.setattr(session, "_is_on_compass_app_page", app_probe)
    monkeypatch.setattr(session, "_is_on_compass_mobile_picker", picker_probe)
    monkeypatch.setattr(session, "_is_on_wwid_screen", wwid_probe)

    result = asyncio.run(session._advance_existing_session_page(page))

    assert result is page
    workshop_ready.assert_awaited_once_with(page)
    page.goto.assert_not_awaited()
    login_probe.assert_not_awaited()
    sso_probe.assert_not_awaited()
    app_probe.assert_not_awaited()
    picker_probe.assert_not_awaited()
    wwid_probe.assert_not_awaited()