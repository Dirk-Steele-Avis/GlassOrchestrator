import json
from datetime import date
from unittest.mock import MagicMock

import pytest

import create_compass_complaints as complaints


class _FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class _FakeRequestContext:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, multipart):
        self.calls.append({"url": url, "multipart": multipart})
        return _FakeResponse(self._responses.pop(0))


class _FakePage:
    def __init__(self, responses):
        self._responses = list(responses)
        self.request = _FakeRequestContext(responses)

    def evaluate(self, script, arg):
        self.request.calls.append({"url": arg["url"], "payload": arg["payload"]})
        payload = self._responses.pop(0)
        return {"ok": True, "status": 200, "text": json.dumps(payload)}


def _candidate(mva: str = "012345678", area: str | None = None) -> complaints.CandidateRow:
    return complaints.CandidateRow(
        row_index=2,
        mva=mva,
        inventory_date_raw="08/20/2026",
        damage_expectation="repair",
        area=area,
    )


def _mock_processing_session(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    context.new_page.return_value = page

    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = context

    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = None

    monkeypatch.setattr(complaints, "sync_playwright", lambda: manager)
    monkeypatch.setattr(complaints, "_open_home_page", MagicMock())
    monkeypatch.setattr(complaints, "_click_vehicles", MagicMock(return_value=page))
    monkeypatch.setattr(complaints, "_wait_for_keyword_search_input", MagicMock())
    monkeypatch.setattr(complaints, "_search_mva", MagicMock())
    return page, context


def test_collect_candidates_filters_today_normalizes_mva_and_damage_type():
    values = [
        ["Inventory Date", "MVA", "Damage Type", "Area"],
        ["08/20/2026", "12345678", "Repair", "Rear View Mirror"],
        ["08/19/2026", "999999999", "Replace", "Windshield"],
        ["08/20/2026", "invalid", "Replace", "Windshield"],
    ]

    candidates, summary = complaints._collect_candidates(values, date(2026, 8, 20))

    assert candidates == [
        complaints.CandidateRow(
            row_index=2,
            mva="012345678",
            inventory_date_raw="08/20/2026",
            damage_expectation="repair",
            area="Rear View Mirror",
        )
    ]
    assert summary.total_rows_read == 3
    assert summary.rows_for_day == 2
    assert summary.skipped_invalid == 1


def test_collect_candidates_processes_only_first_duplicate_occurrence(caplog):
    values = [
        ["Inventory Date", "MVA"],
        ["08/20/2026", "12345678"],
        ["08/20/2026", "12345678"],
    ]

    candidates, summary = complaints._collect_candidates(values, date(2026, 8, 20))

    assert [candidate.mva for candidate in candidates] == ["012345678"]
    assert [candidate.row_index for candidate in candidates] == [2]
    assert summary.rows_for_day == 2
    assert "Row 3 skipped duplicate MVA 012345678" in caplog.text


def test_process_candidates_skips_only_when_complaint_and_work_item_exist(monkeypatch):
    _, context = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(return_value=complaints.LookupResult(True, "glass_damage_found")),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(True, "open_glass_work_item_found")),
    )
    create_complaint = MagicMock()
    create_work_item = MagicMock()
    monkeypatch.setattr(complaints, "_create_complaint_only", create_complaint)
    monkeypatch.setattr(complaints, "_create_work_item_for_glass_complaint", create_work_item)

    summary = complaints._process_candidates([_candidate()], False, complaints.RunSummary())

    assert summary.skipped_existing == 1
    assert summary.created == 0
    assert summary.failed == 0
    create_complaint.assert_not_called()
    create_work_item.assert_not_called()
    context.close.assert_called_once()


def test_process_candidates_does_not_launch_browser_when_empty(monkeypatch):
    playwright = MagicMock()
    monkeypatch.setattr(complaints, "sync_playwright", playwright)
    summary = complaints.RunSummary(total_rows_read=4)

    result = complaints._process_candidates([], False, summary)

    assert result is summary
    playwright.assert_not_called()


def test_process_candidates_adds_work_item_to_existing_complaint(monkeypatch):
    _, _ = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(return_value=complaints.LookupResult(True, "glass_damage_found")),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(False, "no_work_item_tiles")),
    )
    create_complaint = MagicMock()
    create_work_item = MagicMock(return_value=(True, "work_item_created"))
    monkeypatch.setattr(complaints, "_create_complaint_only", create_complaint)
    monkeypatch.setattr(complaints, "_create_work_item_for_glass_complaint", create_work_item)

    summary = complaints._process_candidates([_candidate()], False, complaints.RunSummary())

    assert summary.created == 1
    assert summary.failed == 0
    create_complaint.assert_not_called()
    create_work_item.assert_called_once()


def test_process_candidates_creates_complaint_then_work_item(monkeypatch):
    _, _ = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(return_value=complaints.LookupResult(False, "glass_damage_not_present")),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(False, "no_work_item_tiles")),
    )
    create_complaint = MagicMock(return_value=(True, "new_complaint_visible"))
    create_work_item = MagicMock(return_value=(True, "work_item_created"))
    monkeypatch.setattr(complaints, "_create_complaint_only", create_complaint)
    monkeypatch.setattr(complaints, "_create_work_item_for_glass_complaint", create_work_item)

    summary = complaints._process_candidates(
        [_candidate(area="Rear View Mirror")], False, complaints.RunSummary()
    )

    assert summary.created == 1
    assert summary.failed == 0
    create_complaint.assert_called_once()
    assert create_complaint.call_args.kwargs["damage_expectation"] == "repair"
    assert create_complaint.call_args.kwargs["area"] == "Rear View Mirror"
    create_work_item.assert_called_once()


def test_process_candidates_stops_row_when_complaint_creation_fails(monkeypatch):
    _, _ = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(return_value=complaints.LookupResult(False, "glass_damage_not_present")),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(False, "no_work_item_tiles")),
    )
    monkeypatch.setattr(
        complaints,
        "_create_complaint_only",
        MagicMock(return_value=(False, "complaint_create_failed")),
    )
    create_work_item = MagicMock()
    monkeypatch.setattr(complaints, "_create_work_item_for_glass_complaint", create_work_item)

    summary = complaints._process_candidates([_candidate()], False, complaints.RunSummary())

    assert summary.created == 0
    assert summary.failed == 1
    create_work_item.assert_not_called()


def test_process_candidates_dry_run_performs_no_create_actions(monkeypatch):
    _, _ = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(return_value=complaints.LookupResult(False, "glass_damage_not_present")),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(False, "no_work_item_tiles")),
    )
    create_complaint = MagicMock()
    create_work_item = MagicMock()
    monkeypatch.setattr(complaints, "_create_complaint_only", create_complaint)
    monkeypatch.setattr(complaints, "_create_work_item_for_glass_complaint", create_work_item)

    summary = complaints._process_candidates([_candidate()], True, complaints.RunSummary())

    assert summary.dry_run_would_create == 1
    assert summary.created == 0
    assert summary.failed == 0
    create_complaint.assert_not_called()
    create_work_item.assert_not_called()


def test_process_candidates_continues_after_one_row_raises(monkeypatch):
    _, _ = _mock_processing_session(monkeypatch)
    monkeypatch.setattr(
        complaints,
        "_inspect_glass_complaint",
        MagicMock(
            side_effect=[
                RuntimeError("lookup failed"),
                complaints.LookupResult(True, "glass_damage_found"),
            ]
        ),
    )
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(True, "open_glass_work_item_found")),
    )

    summary = complaints._process_candidates(
        [_candidate("012345678"), _candidate("098765432")],
        False,
        complaints.RunSummary(),
    )

    assert summary.failed == 1
    assert summary.skipped_existing == 1


def test_process_candidates_routes_lookup_through_gated_helper(monkeypatch):
    page, context = _mock_processing_session(monkeypatch)
    lookup = complaints.LookupResult(True, "api_glass_complaint_found", source="api")
    resolve_lookup = MagicMock(return_value=lookup)
    monkeypatch.setattr(complaints, "_resolve_glass_complaint_lookup", resolve_lookup)
    monkeypatch.setattr(
        complaints,
        "_has_open_glass_work_item",
        MagicMock(return_value=(True, "open_glass_work_item_found")),
    )

    runtime_config = {complaints.COMPASS_COMPLAINT_API_FLAG: True}
    summary = complaints._process_candidates(
        [_candidate()],
        False,
        complaints.RunSummary(),
        runtime_config=runtime_config,
    )

    assert summary.skipped_existing == 1
    resolve_lookup.assert_called_once_with(context, page, runtime_config, "012345678")


def test_parse_args_defaults_to_live_spreadsheet_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", ["create_compass_complaints.py"])

    args = complaints._parse_args()

    assert args.mva is None
    assert args.dry_run is False


def test_inspect_glass_complaint_matches_exact_title(monkeypatch):
    table = MagicMock()
    table.inner_text.return_value = "Active Complaints"
    page = MagicMock()
    page.locator.return_value.first = table
    monkeypatch.setattr(complaints, "_read_title_texts", lambda _table: ["Glass Damage"])

    result = complaints._inspect_glass_complaint(page, "012345678")

    assert result.exists is True
    assert result.reason == "glass_damage_found"


def test_inspect_glass_complaint_via_api_filters_to_active_glass_only():
    page = _FakePage(
        [
            [{"$primaryKey": "12189223", "mvaNo": "054019932"}],
            [
                {
                    "damageCategory": "Glass Damage",
                    "status": "Pending",
                    "complaintId": "glass-1",
                },
                {
                    "damageCategory": "Tire Damage",
                    "status": "Pending",
                    "complaintId": "tire-1",
                },
                {
                    "damageCategory": "Glass Damage",
                    "status": "Resolved",
                    "complaintId": "glass-2",
                },
            ],
        ]
    )

    result = complaints._inspect_glass_complaint_via_api(page, {}, "054019932")

    assert result.exists is True
    assert result.reason == "api_glass_complaint_found"
    assert result.source == "api"
    assert result.complaint_ids == ["glass-1"]
    assert result.title_texts == ["Glass Damage"]
    assert page.request.calls[0]["url"].endswith("/sw/get-vehicle-fresh")
    assert page.request.calls[1]["url"].endswith("/sw/get-complaint-fresh")


def test_inspect_glass_complaint_via_api_ignores_active_non_glass_complaints():
    page = _FakePage(
        [
            [{"$primaryKey": "12189223", "mvaNo": "054019932"}],
            [
                {
                    "damageCategory": "Tire Damage",
                    "status": "Pending",
                    "complaintId": "tire-1",
                }
            ],
        ]
    )

    result = complaints._inspect_glass_complaint_via_api(page, {}, "054019932")

    assert result.exists is False
    assert result.reason == "api_glass_complaint_not_present"
    assert result.source == "api"
    assert result.complaint_ids == []


def test_resolve_glass_complaint_lookup_uses_ui_when_flag_disabled(monkeypatch):
    ui_lookup = complaints.LookupResult(True, "glass_damage_found")
    ui_mock = MagicMock(return_value=ui_lookup)
    api_mock = MagicMock()
    monkeypatch.setattr(complaints, "_inspect_glass_complaint", ui_mock)
    monkeypatch.setattr(complaints, "_inspect_glass_complaint_via_api", api_mock)

    result = complaints._resolve_glass_complaint_lookup(MagicMock(), MagicMock(), {}, "012345678")

    assert result is ui_lookup
    ui_mock.assert_called_once()
    api_mock.assert_not_called()


def test_resolve_glass_complaint_lookup_raises_on_api_error(monkeypatch):
    api_mock = MagicMock(side_effect=RuntimeError("boom"))
    ui_mock = MagicMock()
    monkeypatch.setattr(complaints, "_inspect_glass_complaint", ui_mock)
    monkeypatch.setattr(complaints, "_inspect_glass_complaint_via_api", api_mock)

    with pytest.raises(RuntimeError, match="boom"):
        complaints._resolve_glass_complaint_lookup(
            MagicMock(),
            MagicMock(),
            {complaints.COMPASS_COMPLAINT_API_FLAG: True},
            "012345678",
        )

    api_mock.assert_called_once()
    ui_mock.assert_not_called()


def test_vehicle_mva_match_requires_exact_canonical_digits():
    assert not complaints._vehicle_mva_matches("59379600", "059379600")
    assert complaints._vehicle_mva_matches("059379600", "059379600")
    assert not complaints._vehicle_mva_matches("59520156", "059379600")


def test_vehicle_details_waits_until_mva_property_matches():
    page = MagicMock()
    mva_rows = MagicMock()
    mva_row = MagicMock()
    values = MagicMock()
    value = MagicMock()
    page.locator.return_value = mva_rows
    mva_rows.count.return_value = 1
    mva_rows.first = mva_row
    mva_row.locator.return_value = values
    values.count.return_value = 1
    values.first = value
    value.inner_text.side_effect = ["016403096", "059379600"]

    complaints._wait_for_vehicle_details_mva(page, "059379600")

    assert value.inner_text.call_count == 2
    page.wait_for_timeout.assert_called_once_with(400)


def test_select_vehicle_search_result_waits_for_exact_result():
    page = MagicMock()
    table = MagicMock()
    titles = MagicMock()
    exact_titles = MagicMock()
    exact_title = MagicMock()
    page.locator.return_value.first = table
    table.locator.return_value = titles
    titles.filter.return_value = exact_titles
    exact_titles.count.side_effect = [0, 0, 1]
    exact_titles.first = exact_title
    exact_title.is_visible.return_value = True

    complaints._select_vehicle_search_result(page, "059379600")

    assert exact_titles.count.call_count == 3
    exact_title.click.assert_called_once_with(timeout=8000)
    assert page.wait_for_timeout.call_args_list[:2] == [
        ((250,), {}),
        ((250,), {}),
    ]


def test_search_mva_requires_confirmed_vehicle_details(monkeypatch):
    page = MagicMock()
    by_mva = MagicMock()
    page.get_by_role.return_value.first = by_mva
    keyword = MagicMock()
    select_result = MagicMock()
    confirm_details = MagicMock(side_effect=RuntimeError("MVA mismatch"))
    monkeypatch.setattr(complaints, "_wait_for_keyword_search_input", lambda _page: keyword)
    monkeypatch.setattr(complaints, "_select_vehicle_search_result", select_result)
    monkeypatch.setattr(complaints, "_wait_for_vehicle_details_mva", confirm_details)

    try:
        complaints._search_mva(page, "059379600")
        assert False, "Expected mismatched vehicle details to fail"
    except RuntimeError as exc:
        assert str(exc) == "MVA mismatch"

    by_mva.click.assert_called_once_with(timeout=5000)
    keyword.fill.assert_called_once_with("059379600")
    keyword.press.assert_called_once_with("Enter")
    select_result.assert_called_once_with(page, "059379600")
    confirm_details.assert_called_once_with(page, "059379600")


def test_has_open_glass_work_item_accepts_glass_complaint_attached_count(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(complaints, "_find_glass_row_index_blueprint", lambda _page: 4)
    monkeypatch.setattr(
        complaints,
        "_attached_work_items_count_blueprint",
        lambda _page, _row_index: 1,
    )

    exists, reason = complaints._has_open_glass_work_item(page)

    assert exists is True
    assert reason == "glass_complaint_attached_work_items=1"


def test_has_open_glass_work_item_rejects_zero_on_glass_complaint(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(complaints, "_find_glass_row_index_blueprint", lambda _page: 4)
    monkeypatch.setattr(
        complaints,
        "_attached_work_items_count_blueprint",
        lambda _page, _row_index: 0,
    )

    exists, reason = complaints._has_open_glass_work_item(page)

    assert exists is False
    assert reason == "glass_complaint_attached_work_items=0"
    page.locator.assert_not_called()


def test_run_returns_nonzero_when_any_candidate_fails(monkeypatch):
    monkeypatch.setattr(complaints, "_setup_logging", MagicMock())
    monkeypatch.setattr(
        complaints,
        "_parse_args",
        MagicMock(return_value=MagicMock(mva="012345678", dry_run=False)),
    )
    monkeypatch.setattr(
        complaints,
        "_build_single_candidate",
        MagicMock(return_value=([_candidate()], complaints.RunSummary(total_rows_read=1, rows_for_day=1))),
    )
    monkeypatch.setattr(
        complaints,
        "_process_candidates",
        MagicMock(return_value=complaints.RunSummary(total_rows_read=1, rows_for_day=1, failed=1)),
    )

    assert complaints.run() == 1


def test_run_returns_zero_when_all_candidates_succeed(monkeypatch):
    monkeypatch.setattr(complaints, "_setup_logging", MagicMock())
    monkeypatch.setattr(
        complaints,
        "_parse_args",
        MagicMock(return_value=MagicMock(mva="012345678", dry_run=False)),
    )
    monkeypatch.setattr(
        complaints,
        "_build_single_candidate",
        MagicMock(return_value=([_candidate()], complaints.RunSummary(total_rows_read=1, rows_for_day=1))),
    )
    monkeypatch.setattr(
        complaints,
        "_process_candidates",
        MagicMock(return_value=complaints.RunSummary(total_rows_read=1, rows_for_day=1, created=1)),
    )

    assert complaints.run() == 0