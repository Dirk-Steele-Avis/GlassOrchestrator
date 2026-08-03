import os

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
