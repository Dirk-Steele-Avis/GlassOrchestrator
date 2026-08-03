"""
Unit tests — create_workitem.py always uses launch_persistent_context with the
Edge profile directory, matching the pattern used by close_workitem.py and
verify_workitem.py.

GLASS_EDGE_NO_PROFILE is ignored by all async Playwright scripts; it only
affects the legacy sync playwright_driver_manager.py path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pw_mock():
    """Return (pw_mock, async_playwright_factory) for patching async_playwright."""
    page = AsyncMock()
    context = AsyncMock()
    context.pages = []
    context.close = AsyncMock()

    pw = MagicMock()
    pw.chromium.launch = AsyncMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=context)

    async_pw_cm = AsyncMock()
    async_pw_cm.__aenter__ = AsyncMock(return_value=pw)
    async_pw_cm.__aexit__ = AsyncMock(return_value=None)

    return pw, async_pw_cm, context, page


class TestBrowserLaunchMode:
    """_run_playwright_creation_async always uses launch_persistent_context with a profile."""

    def _run_async(self, monkeypatch, pw, async_pw_cm, context, page, env_no_profile: str | None = None):
        import create_workitem as cw

        if env_no_profile is not None:
            monkeypatch.setenv("GLASS_EDGE_NO_PROFILE", env_no_profile)
        else:
            monkeypatch.delenv("GLASS_EDGE_NO_PROFILE", raising=False)

        monkeypatch.setattr("create_workitem.async_playwright", lambda: async_pw_cm)
        monkeypatch.setattr("create_workitem.pw_warmup_compass", AsyncMock())
        monkeypatch.setattr(
            "create_workitem.ensure_profile_context",
            AsyncMock(return_value=(context, page)),
        )

        with pytest.raises(SystemExit):
            asyncio.run(cw._run_playwright_creation_async([]))

    def test_always_uses_persistent_context(self, monkeypatch):
        """launch_persistent_context is always used — never browser.launch()."""
        pw, async_pw_cm, context, page = _make_pw_mock()
        self._run_async(monkeypatch, pw, async_pw_cm, context, page)

        pw.chromium.launch_persistent_context.assert_called_once()
        pw.chromium.launch.assert_not_called()

    def test_persistent_context_used_even_when_glass_edge_no_profile_set(self, monkeypatch):
        """GLASS_EDGE_NO_PROFILE=1 is ignored — still uses launch_persistent_context."""
        pw, async_pw_cm, context, page = _make_pw_mock()
        self._run_async(monkeypatch, pw, async_pw_cm, context, page, env_no_profile="1")

        pw.chromium.launch_persistent_context.assert_called_once()
        pw.chromium.launch.assert_not_called()

    def test_profile_directory_arg_passed_to_launch(self, monkeypatch):
        """--profile-directory is included in the args passed to launch_persistent_context."""
        pw, async_pw_cm, context, page = _make_pw_mock()
        self._run_async(monkeypatch, pw, async_pw_cm, context, page)

        call_kwargs = pw.chromium.launch_persistent_context.call_args
        args_list = call_kwargs.kwargs.get("args", [])
        assert any("--profile-directory" in a for a in args_list), (
            f"--profile-directory not found in launch args: {args_list}"
        )

    def test_chromium_sandbox_enabled_for_edge_profile(self, monkeypatch):
        """Edge persistent profile launch should explicitly enable Chromium sandboxing."""
        pw, async_pw_cm, context, page = _make_pw_mock()
        self._run_async(monkeypatch, pw, async_pw_cm, context, page)

        call_kwargs = pw.chromium.launch_persistent_context.call_args
        assert call_kwargs.kwargs.get("chromium_sandbox") is True, (
            f"chromium_sandbox was not enabled on launch: {call_kwargs.kwargs}"
        )

    def test_ensure_profile_context_called(self, monkeypatch):
        """ensure_profile_context() is called to advance session after launch."""
        import create_workitem as cw
        pw, async_pw_cm, context, page = _make_pw_mock()

        ensure_profile = AsyncMock(return_value=(context, page))
        monkeypatch.setattr("create_workitem.async_playwright", lambda: async_pw_cm)
        monkeypatch.setattr("create_workitem.pw_warmup_compass", AsyncMock())
        monkeypatch.setattr("create_workitem.ensure_profile_context", ensure_profile)

        with pytest.raises(SystemExit):
            asyncio.run(cw._run_playwright_creation_async([]))

        ensure_profile.assert_called_once()
