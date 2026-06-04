"""VehicleDetailsPage tests using fixture HTML loaded into a real Page.

Requires Playwright + a Chromium install (`playwright install chromium`).
"""
import pytest

from src.compass_go.pages.vehicle_details_page import (
    DATA_KEY_DESC,
    DATA_KEY_MVA,
    DATA_KEY_VIN,
    VehicleDetailsPage,
)
from tests.compass_go.fixtures import (
    VEHICLE_DETAILS_COLLAPSED_HTML,
    VEHICLE_DETAILS_EXPANDED_HTML,
)

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


def test_read_vin_from_fixture(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)
    details = VehicleDetailsPage(page)

    assert details.read(DATA_KEY_VIN) == "5XYP64GC1SG682257"


def test_read_mva_from_fixture(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)
    details = VehicleDetailsPage(page)

    assert details.read(DATA_KEY_MVA) == "058883134"


def test_read_desc_from_fixture(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)
    details = VehicleDetailsPage(page)

    assert details.read(DATA_KEY_DESC) == "NISSROGU"


def test_expand_show_more_is_noop_when_already_expanded(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)
    details = VehicleDetailsPage(page)

    details.expand_show_more()  # Show Less is visible; must not raise


def test_expand_show_more_clicks_when_collapsed(page):
    page.set_content(VEHICLE_DETAILS_COLLAPSED_HTML)
    # Swap the toggle text to "Show Less" on click so the wait_for() resolves.
    page.evaluate(
        """() => {
            const btn = [...document.querySelectorAll('button')]
                .find(b => b.textContent.trim() === 'Show More');
            btn.addEventListener('click', () => { btn.textContent = 'Show Less'; });
        }"""
    )
    details = VehicleDetailsPage(page)

    details.expand_show_more()

    assert page.get_by_role("button", name="Show Less", exact=True).is_visible()


def test_back_clicks_back_button(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)
    page.evaluate(
        """() => {
            const btn = document.querySelector('button.back-button');
            btn.addEventListener('click', () => { document.title = 'BACK_CLICKED'; });
        }"""
    )
    details = VehicleDetailsPage(page)

    details.back()

    assert page.title() == "BACK_CLICKED"
