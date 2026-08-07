import os
import time

from src.compass_go.auth_flow import AuthFlow


def _expected_entry_url() -> str:
    return os.getenv("COMPASS_GO_ENTRY_URL", "https://go.avisbudget.palantirfoundry.com/")


class FakeContext:
    def __init__(self, pages=None, replacement=None):
        self.pages = list(pages or [])
        self._replacement = replacement
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        if self._replacement is None:
            raise RuntimeError("no replacement page configured")
        self.pages.append(self._replacement)
        return self._replacement


class FakePage:
    def __init__(self, context, closed=False, url="https://example.invalid/"):
        self.context = context
        self._closed = closed
        self.url = url
        self.goto_calls = []

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))


def test_ensure_page_alive_switches_to_existing_open_page():
    replacement = FakePage(context=None, closed=False, url="https://replacement/")
    context = FakeContext(pages=[])
    replacement.context = context

    closed_page = FakePage(context=context, closed=True, url="https://closed/")
    context.pages = [closed_page, replacement]

    flow = AuthFlow(closed_page)

    flow._ensure_page_alive()

    assert flow._page is replacement
    assert context.new_page_calls == 0


def test_ensure_page_alive_recreates_page_when_none_open():
    replacement = FakePage(context=None, closed=False, url="")
    context = FakeContext(pages=[], replacement=replacement)
    replacement.context = context

    closed_page = FakePage(context=context, closed=True, url="https://go.avisbudget.palantirfoundry.com/")
    context.pages = [closed_page]

    flow = AuthFlow(closed_page)

    flow._ensure_page_alive()

    assert flow._page is replacement
    assert context.new_page_calls == 1
    assert replacement.goto_calls == [
        (_expected_entry_url(), "domcontentloaded")
    ]


def test_ensure_page_alive_renavigates_when_switching_to_about_blank():
    replacement = FakePage(context=None, closed=False, url="about:blank")
    context = FakeContext(pages=[])
    replacement.context = context

    closed_page = FakePage(context=context, closed=True, url="https://closed/")
    context.pages = [closed_page, replacement]

    flow = AuthFlow(closed_page)

    flow._ensure_page_alive()

    assert flow._page is replacement
    assert replacement.goto_calls == [
        (_expected_entry_url(), "domcontentloaded")
    ]


def test_adopt_best_context_page_prefers_scan_url_over_blank():
    blank = FakePage(context=None, closed=False, url="about:blank")
    scan = FakePage(
        context=None,
        closed=False,
        url="https://go.avisbudget.palantirfoundry.com/scan/vehicle-details/",
    )
    context = FakeContext(pages=[])
    blank.context = context
    scan.context = context
    context.pages = [blank, scan]

    flow = AuthFlow(blank)

    flow._adopt_best_context_page()

    assert flow._page is scan


def test_ensure_signed_in_handles_quick_fix_before_scan(monkeypatch):
    page = type("Page", (), {})()
    page.context = type("Ctx", (), {"pages": [page]})()
    page.is_closed = lambda: False
    page.url = "https://go.avisbudget.palantirfoundry.com/"

    flow = AuthFlow(page)

    # Avoid real sleeping during loop.
    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Simulate quick-fix once, then scan ready.
    quick_fix_calls = {"count": 0}

    def _handle_quick_fix_once():
        quick_fix_calls["count"] += 1
        return quick_fix_calls["count"] == 1

    monkeypatch.setattr(flow, "_adopt_best_context_page", lambda: None)
    monkeypatch.setattr(flow, "_ensure_page_alive", lambda: None)
    monkeypatch.setattr(flow, "_handle_quick_fix_if_needed", _handle_quick_fix_once)

    class _Scan:
        def __init__(self):
            self.calls = 0

        def is_displayed(self):
            self.calls += 1
            return self.calls >= 1 and quick_fix_calls["count"] > 1

    scan = _Scan()
    flow._scan = scan
    flow._login = type("Login", (), {"is_displayed": lambda self: False})()
    flow._location = type("Loc", (), {"is_displayed": lambda self: False})()

    result = flow.ensure_signed_in()

    assert result is scan
    assert quick_fix_calls["count"] >= 2


def test_handle_quick_fix_raises_when_persistent_after_clear(monkeypatch):
    page = type("Page", (), {})()
    page.context = type("Ctx", (), {"pages": [page]})()
    page.is_closed = lambda: False
    page.url = "https://go.avisbudget.palantirfoundry.com/"

    flow = AuthFlow(page)
    flow._quick_fix_cleared = True
    flow._quick_fix_attempts = 3

    class _Panel:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def is_visible(self, timeout=None):
            return True

    page.locator = lambda *_args, **_kwargs: _Panel()

    try:
        flow._handle_quick_fix_if_needed()
        assert False, "Expected RuntimeError for persistent quick-fix state"
    except RuntimeError as exc:
        assert "persisted after clear app data" in str(exc)
