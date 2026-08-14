"""Tests for outcome detectors and the MVANotFoundError exception."""
import pytest

from src.compass_go.outcomes import (
    DEFAULT_DETECTORS,
    MVANotFoundError,
    Outcome,
    detect_details_ready,
    detect_mva_not_found,
)
from tests.compass_go.fixtures import (
    VEHICLE_DETAILS_EXPANDED_HTML,
    VEHICLE_NOT_FOUND_HTML,
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


def test_detect_mva_not_found_fires_on_error_card(page):
    page.set_content(VEHICLE_NOT_FOUND_HTML)

    assert detect_mva_not_found(page) is Outcome.MVA_NOT_FOUND


def test_detect_mva_not_found_returns_none_on_details(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)

    assert detect_mva_not_found(page) is None


def test_app_not_installed_status_does_not_mean_mva_not_found(page):
    page.set_content(
        VEHICLE_DETAILS_EXPANDED_HTML.replace(
            "<body>",
            "<body><button><span>App not installed.</span></button>",
        )
    )

    assert detect_mva_not_found(page) is None
    assert detect_details_ready(page) is Outcome.DETAILS_READY


def test_detect_details_ready_fires_on_details_heading(page):
    page.set_content(VEHICLE_DETAILS_EXPANDED_HTML)

    assert detect_details_ready(page) is Outcome.DETAILS_READY


def test_detect_details_ready_waits_for_loaded_mva_cell(page):
    page.set_content(
        """<h2>Vehicle Details</h2><table><tr>
        <td data-key="mvaNo.1" role="gridcell"><div class="skeleton"></div></td>
        </tr></table>"""
    )

    assert detect_details_ready(page) is None


def test_default_detectors_priority_error_before_success(page):
    """If both signals are on the page, the error detector must win."""
    # The not-found fixture already contains the Vehicle Details heading
    # alongside the error card — this is exactly what Compass GO renders.
    page.set_content(VEHICLE_NOT_FOUND_HTML)

    fired = None
    for detector in DEFAULT_DETECTORS:
        result = detector(page)
        if result is not None:
            fired = result
            break
    assert fired is Outcome.MVA_NOT_FOUND


def test_mva_not_found_error_carries_mva():
    err = MVANotFoundError("12345678")

    assert "12345678" in str(err)
