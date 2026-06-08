"""ScanPage tests using fixture HTML loaded into a real Page."""
import pytest

from src.compass_go.outcomes import MVANotFoundError
from src.compass_go.pages.scan_page import ScanPage
from tests.compass_go.fixtures import SCAN_VEHICLE_HTML

pytestmark = pytest.mark.browser

pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment-dependent
            pytest.skip(
                f"Chromium not available for Playwright: {exc}. "
                "Run '.venv\\Scripts\\python.exe -m playwright install chromium'."
            )
        context = browser.new_context()
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def test_is_displayed_true_when_begin_scanning_visible(page):
    page.set_content(SCAN_VEHICLE_HTML)

    assert ScanPage(page).is_displayed() is True


def test_submit_clicks_begin_then_fills_input_and_clicks_enter(page):
    page.set_content(SCAN_VEHICLE_HTML)
    page.evaluate(
        """() => {
            const begin = document.getElementById('begin-scanning');
            begin.addEventListener('click', () => begin.remove());
            const btn = [...document.querySelectorAll('button')]
                .find(b => b.textContent.trim() === 'Enter');
            btn.addEventListener('click', () => {
                const i = document.querySelector('input[aria-label="Or enter MVA/VIN"]');
                document.title = 'SUBMITTED:' + i.value;
                document.body.insertAdjacentHTML('beforeend', '<h1>Vehicle Details</h1>');
            });
        }"""
    )

    ScanPage(page).submit("058883134")

    assert page.title() == "SUBMITTED:058883134"


def test_submit_raises_mva_not_found_when_error_card_appears(page, monkeypatch):
    # Tighten the outcome timeout so a test regression fails fast rather than
    # blocking on the production 20s default.
    monkeypatch.setenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "3")
    page.set_content(SCAN_VEHICLE_HTML)
    page.evaluate(
        """() => {
            const begin = document.getElementById('begin-scanning');
            begin.addEventListener('click', () => begin.remove());
            const btn = [...document.querySelectorAll('button')]
                .find(b => b.textContent.trim() === 'Enter');
            btn.addEventListener('click', () => {
                document.body.insertAdjacentHTML(
                    'beforeend',
                    '<div><span>Vehicle Not Found</span></div>'
                );
            });
        }"""
    )

    with pytest.raises(MVANotFoundError) as exc_info:
        ScanPage(page).submit("99999999")

    assert "99999999" in str(exc_info.value)
