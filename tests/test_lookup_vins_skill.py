"""Focused contract tests for the read-only chat lookup skill."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github" / "skills" / "lookup-vins" / "scripts" / "lookup_vins.py"
)
SPEC = importlib.util.spec_from_file_location("lookup_vins_skill", SCRIPT)
lookup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lookup)

VIN = "1HGCY1F43SA091270"


@pytest.fixture(scope="module")
def browser():
    with lookup.sync_playwright() as pw:
        instance = pw.chromium.launch(channel="msedge", headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(lookup, "WAIT_MS", 500)
    context = browser.new_context()
    result = context.new_page()
    result.set_default_timeout(500)
    yield result
    context.close()


def set_vehicle_app(page, *, returned_mva=None, vin=VIN, not_found=False,
                    found_on_retry=False, bad_retry_vin=False):
    page.set_content("""
        <section id="scan">
          <input aria-label="Or enter MVA/VIN/Plate">
          <button id="enter">Enter</button>
        </section>
        <section id="details" hidden>
          <h1>Vehicle Details <button>Open Help</button></h1>
          <div id="result"></div>
          <button class="back-button">Back</button>
        </section>
        <script>
          window.submissions = [];
          document.querySelector('#enter').onclick = () => {
            const mva = document.querySelector('input').value;
            window.submissions.push(mva);
            document.querySelector('#scan').hidden = true;
            document.querySelector('#details').hidden = false;
            document.querySelector('#result').innerHTML =
              window.missing && mva === '00000000' &&
                (!window.foundOnRetry || window.submissions.filter(value => value === mva).length === 1)
                ? '<p>Vehicle Not Found</p>'
                : `<table><tr><td data-key="mvaNo.1" role="gridcell">${window.returnedMva ?? '0' + mva}</td></tr>
                    <tr id="vin" hidden><td data-key="vinNo.1" role="gridcell">${window.badRetryVin && mva === '00000000' ? 'invalid' : window.vinValue}</td></tr></table>
                   <button id="more">Show More</button>`;
            const more = document.querySelector('#more');
            if (more) more.onclick = () => {
              document.querySelector('#vin').hidden = false;
              more.innerText = 'Show Less';
            };
          };
          document.querySelector('.back-button').onclick = () => {
            document.querySelector('#result').innerHTML = '';
            document.querySelector('#details').hidden = true;
            document.querySelector('#scan').hidden = false;
          };
        </script>
    """)
    page.evaluate(
        "values => Object.assign(window, values)",
        {"returnedMva": returned_mva, "vinValue": vin, "missing": not_found,
         "foundOnRetry": found_on_retry, "badRetryVin": bad_retry_vin},
    )


@pytest.mark.parametrize("mvas", [[], ["1234567"], ["123456789"], ["１２３４５６７８"],
                                      ["60292282;"], [" 60292282"], ["MVA"]])
def test_invalid_inputs_never_start_browser(mvas, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(lookup, "sync_playwright", start)
    with pytest.raises(ValueError):
        lookup.run_lookup(mvas)
    start.assert_not_called()


def test_lookup_preserves_order_duplicates_and_zeros(page):
    set_vehicle_app(page)
    mvas = ["01234567", "60292282", "01234567"]
    assert lookup.lookup_mvas(page, mvas) == [(mva, VIN) for mva in mvas]
    assert page.evaluate("window.submissions") == mvas


def test_explicit_not_found_is_nonfatal(page, capsys):
    set_vehicle_app(page, not_found=True)
    assert lookup.lookup_mvas(page, ["00000000", "60292282"]) == [
        ("00000000", "N/A"), ("60292282", VIN)
    ]
    assert page.evaluate("window.submissions") == ["00000000", "60292282", "00000000"]
    errors = capsys.readouterr().err
    assert "queued for one end-of-list retry" in errors
    assert "still Vehicle Not Found after one retry; returning N/A" in errors


def test_not_found_retry_recovers_vin_at_end_in_original_output_order(page, capsys):
    set_vehicle_app(page, not_found=True, found_on_retry=True)
    assert lookup.lookup_mvas(page, ["00000000", "60292282"]) == [
        ("00000000", VIN), ("60292282", VIN)
    ]
    assert page.evaluate("window.submissions") == ["00000000", "60292282", "00000000"]
    assert "VIN found on end-of-list retry" in capsys.readouterr().err


def test_duplicate_not_found_occurrences_keep_order_and_one_retry_each(page):
    set_vehicle_app(page, not_found=True)
    mvas = ["00000000", "60292282", "00000000"]
    assert lookup.lookup_mvas(page, mvas) == [
        ("00000000", "N/A"), ("60292282", VIN), ("00000000", "N/A")
    ]
    assert page.evaluate("window.submissions") == mvas + ["00000000", "00000000"]


def test_retry_error_stops_batch_without_stdout(page, monkeypatch, capsys):
    set_vehicle_app(page, not_found=True, found_on_retry=True, bad_retry_vin=True)
    monkeypatch.setattr(lookup, "run_lookup", lambda mvas: lookup.lookup_mvas(page, mvas))
    assert lookup.main(["00000000", "60292282"]) == 1
    assert page.evaluate("window.submissions") == ["00000000", "60292282", "00000000"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "MVA 00000000 / end-of-list retry / read result" in output.err


@pytest.mark.parametrize("returned_mva,vin", [
    ("061004403", VIN), (None, ""), (None, "not-a-vin"), (None, "I" * 17),
])
def test_bad_result_stops_before_next_submission(page, returned_mva, vin):
    set_vehicle_app(page, returned_mva=returned_mva, vin=vin)
    with pytest.raises(lookup.LookupFailure, match="MVA 60292282 / read result"):
        lookup.lookup_mvas(page, ["60292282", "61004403"])
    assert page.evaluate("window.submissions") == ["60292282"]


def test_main_exact_output(monkeypatch, capsys):
    monkeypatch.setattr(lookup, "run_lookup", lambda mvas: [(mva, VIN) for mva in mvas])
    assert lookup.main(["01234567", "60292282"]) == 0
    output = capsys.readouterr()
    assert output.out == f"MVA VIN\n01234567 {VIN}\n60292282 {VIN}\n"
    assert output.err == ""


def test_main_failure_has_no_partial_success(monkeypatch, capsys):
    def fail(mvas):
        raise lookup.LookupFailure("MVA 61004403 / read result: mismatched MVA")

    monkeypatch.setattr(lookup, "run_lookup", fail)
    assert lookup.main(["60292282", "61004403"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "MVA 61004403 / read result: mismatched MVA" in output.err


@pytest.mark.parametrize("profile", ["", " ", str(lookup.DEFAULT_EDGE_USER_DATA_DIR)])
def test_unsafe_profile_rejected_before_launch(profile, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", profile)
    start = MagicMock()
    monkeypatch.setattr(lookup, "sync_playwright", start)
    with pytest.raises(lookup.LookupFailure):
        lookup.run_lookup(["60292282"])
    start.assert_not_called()


def test_launch_failure_is_one_attempt_without_recovery(monkeypatch):
    pw = MagicMock()
    pw.chromium.launch_persistent_context.side_effect = lookup.Error("profile in use")
    start = MagicMock()
    start.return_value.__enter__.return_value = pw
    monkeypatch.setattr(lookup, "sync_playwright", start)
    monkeypatch.delenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_EDGE_PROFILE_DIRECTORY", raising=False)
    with pytest.raises(lookup.LookupFailure, match="launch Edge profile.*profile in use"):
        lookup.run_lookup(["60292282"])
    pw.chromium.launch_persistent_context.assert_called_once()


def test_prepare_scan_timeout_is_explicit(page, monkeypatch):
    monkeypatch.setattr(lookup, "AUTH_SECONDS", 0)
    page.set_content("<h1>Sign in required</h1>")
    with pytest.raises(lookup.LookupFailure, match="Scan tab was not ready"):
        lookup.prepare_scan(page)


def test_prepare_scan_uses_scan_tab_and_begin_gate(page):
    page.set_content("""
        <button role="tab" data-key="scan" onclick="document.querySelector('#begin').hidden=false">Scan</button>
        <button id="begin" hidden onclick="this.hidden=true;document.querySelector('input').hidden=false">Begin Scanning</button>
        <input aria-label="Or enter MVA/VIN/Plate" hidden>
    """)
    lookup.prepare_scan(page)
    assert page.get_by_label("Or enter MVA/VIN/Plate", exact=True).is_visible()


def test_old_input_label_is_rejected_without_submitting(page, monkeypatch):
    monkeypatch.setattr(lookup, "WAIT_MS", 0)
    page.set_content("""
        <button role="tab" data-key="scan">Scan</button>
        <input aria-label="Or enter MVA/VIN">
        <button onclick="window.submitted=true">Enter</button>
    """)
    with pytest.raises(lookup.LookupFailure, match="MVA input"):
        lookup.prepare_scan(page)
    assert page.evaluate("window.submitted === undefined")
