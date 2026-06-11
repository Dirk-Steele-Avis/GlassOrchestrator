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
    # blocking on the production default timeout.
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


def test_submit_dismisses_stale_not_found_card_via_back_arrow(page, monkeypatch):
    """Persistent Edge profile can land on a stale 'Vehicle Not Found' card.
    submit() must click the back arrow to reveal the MVA input before filling.
    """
    monkeypatch.setenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "3")
    # Page starts on the stale not-found card with a back arrow. Clicking
    # the back arrow swaps in the normal Scan view with the MVA input.
    page.set_content(
        """<html><body>
        <button class="back-button" type="button">&lt;</button>
        <h2>Vehicle Details</h2>
        <div class="error-card"><span>Vehicle Not Found</span></div>
        </body></html>"""
    )
    page.evaluate(
        """() => {
            const back = document.querySelector('button.back-button');
            back.addEventListener('click', () => {
                document.body.innerHTML = `
                    <div class="enter-mva-vin">
                      <input aria-label="Or enter MVA/VIN" type="text" />
                      <button type="button">Enter</button>
                    </div>`;
                const btn = document.querySelector('button');
                btn.addEventListener('click', () => {
                    const i = document.querySelector('input[aria-label="Or enter MVA/VIN"]');
                    document.title = 'SUBMITTED:' + i.value;
                    document.body.insertAdjacentHTML(
                        'beforeend', '<h1>Vehicle Details</h1>'
                    );
                });
            });
        }"""
    )

    ScanPage(page).submit("12345678")

    assert page.title() == "SUBMITTED:12345678"


def test_submit_dismisses_stale_vehicle_details_via_back_arrow(page, monkeypatch):
    """If the profile reopens on plain Vehicle Details, submit() must back out
    to Scan before waiting for/filling the MVA input.
    """
    monkeypatch.setenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "3")
    page.set_content(
        """<html><body>
        <button class="back-button" type="button">&lt;</button>
        <h2>Vehicle Details</h2>
        <div><span>Some previous vehicle detail content</span></div>
        </body></html>"""
    )
    page.evaluate(
        """() => {
            const back = document.querySelector('button.back-button');
            back.addEventListener('click', () => {
                document.body.innerHTML = `
                    <div class="enter-mva-vin">
                      <input aria-label="Or enter MVA/VIN" type="text" />
                      <button type="button">Enter</button>
                    </div>`;
                const btn = document.querySelector('button');
                btn.addEventListener('click', () => {
                    const i = document.querySelector('input[aria-label="Or enter MVA/VIN"]');
                    document.title = 'SUBMITTED:' + i.value;
                    document.body.insertAdjacentHTML(
                        'beforeend', '<h1>Vehicle Details</h1>'
                    );
                });
            });
        }"""
    )

    ScanPage(page).submit("87654321")

    assert page.title() == "SUBMITTED:87654321"


def test_submit_waits_for_slow_input_render_with_1s_poll(page, monkeypatch):
    monkeypatch.setenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "5")
    page.set_content("<html><body><div id='root'></div></body></html>")
    page.evaluate(
        """() => {
            setTimeout(() => {
                document.getElementById('root').innerHTML = `
                    <div class="enter-mva-vin">
                      <input aria-label="Or enter MVA/VIN" type="text" />
                      <button type="button">Enter</button>
                    </div>`;
                const btn = [...document.querySelectorAll('button')]
                    .find(b => b.textContent.trim() === 'Enter');
                btn.addEventListener('click', () => {
                    const i = document.querySelector('input[aria-label="Or enter MVA/VIN"]');
                    document.title = 'SUBMITTED:' + i.value;
                    document.body.insertAdjacentHTML('beforeend', '<h1>Vehicle Details</h1>');
                });
            }, 2200);
        }"""
    )

    ScanPage(page).submit("44444444")

    assert page.title() == "SUBMITTED:44444444"


def test_submit_uses_input_timeout_override(page, monkeypatch):
    monkeypatch.setenv("COMPASS_GO_OUTCOME_TIMEOUT_S", "2")
    monkeypatch.setenv("COMPASS_GO_INPUT_TIMEOUT_S", "5")
    page.set_content("<html><body><div id='root'></div></body></html>")
    page.evaluate(
        """() => {
            setTimeout(() => {
                document.getElementById('root').innerHTML = `
                    <div class="enter-mva-vin">
                      <input aria-label="Or enter MVA/VIN" type="text" />
                      <button type="button">Enter</button>
                    </div>`;
                const btn = [...document.querySelectorAll('button')]
                    .find(b => b.textContent.trim() === 'Enter');
                btn.addEventListener('click', () => {
                    const i = document.querySelector('input[aria-label="Or enter MVA/VIN"]');
                    document.title = 'SUBMITTED:' + i.value;
                    document.body.insertAdjacentHTML('beforeend', '<h1>Vehicle Details</h1>');
                });
            }, 3200);
        }"""
    )

    ScanPage(page).submit("55555555")

    assert page.title() == "SUBMITTED:55555555"
