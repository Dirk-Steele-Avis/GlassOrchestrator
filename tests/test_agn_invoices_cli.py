"""Tests for the AGN invoice command-line interface."""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from outlook import agn_invoices


def test_load_excluded_standard_price_skus_is_strict_and_normalizes():
    assert agn_invoices._load_excluded_standard_price_skus({
        "excluded_standard_price_skus": ["dw03055gtyn", "FW06423GTY"],
    }) == frozenset({"DW03055GTY", "FW06423GTY"})

    with pytest.raises(ValueError, match="must be a non-empty list"):
        agn_invoices._load_excluded_standard_price_skus({})
    with pytest.raises(ValueError, match="Invalid excluded"):
        agn_invoices._load_excluded_standard_price_skus({
            "excluded_standard_price_skus": ["DW123"],
        })
    with pytest.raises(ValueError, match="Duplicate excluded"):
        agn_invoices._load_excluded_standard_price_skus({
            "excluded_standard_price_skus": ["DW03055GTY", "dw03055gtyn"],
        })


def test_load_po_numbers_is_strict_and_deduplicates(tmp_path):
    po_file = tmp_path / "po_numbers.txt"
    po_file.write_text("FPO1059300\nfpo1059310\nFPO1059300\n", encoding="utf-8")

    assert agn_invoices.load_po_numbers(po_file) == ["FPO1059300", "FPO1059310"]

    po_file.write_text("FPO1059300\nPO-INVALID\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        agn_invoices.load_po_numbers(po_file)


def test_load_direct_approval_targets_normalizes_and_validates(tmp_path):
    target_file = tmp_path / "targets.txt"
    target_file.write_text(
        "MVA_NUMBER\tVIN_NO\tPO_NUMBER\n"
        "53791706\tKMHLM4DG4RU662228\tFPO1059300\n",
        encoding="utf-8",
    )

    assert agn_invoices.load_direct_approval_targets(target_file) == [
        {
            "mva": "053791706",
            "vin": "KMHLM4DG4RU662228",
            "po_number": "FPO1059300",
        }
    ]


def test_validate_invoice_target_requires_vin_and_mva_match():
    target = {
        "mva": "053791706",
        "vin": "KMHLM4DG4RU662228",
        "po_number": "FPO1059300",
    }

    agn_invoices.validate_invoice_target(target, "KMHLM4DG4RU662228", "53791706")

    with pytest.raises(LookupError, match="Invoice VIN mismatch"):
        agn_invoices.validate_invoice_target(target, "3FMTK3SU1PMA97379", "53791706")
    with pytest.raises(LookupError, match="FieldPO MVA mismatch"):
        agn_invoices.validate_invoice_target(target, "KMHLM4DG4RU662228", "54036953")


def test_min_age_is_ignored_unless_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(
        agn_invoices,
        "ALLOWED_WORK_ORDER_CREATED_BY",
        ["Steele, Dirk"],
    )
    invoice_date = date(2026, 8, 25)

    default_result = agn_invoices.evaluate_closure(
        invoice_date=invoice_date,
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="59037075",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 30),
    )
    override_result = agn_invoices.evaluate_closure(
        invoice_date=invoice_date,
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="59037075",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 30),
        min_age_days=5,
    )

    assert default_result["decision"] == "WOULD_CLOSE"
    assert default_result["reasons"] == []
    assert override_result["decision"] == "WOULD_CLOSE"

    too_young_result = agn_invoices.evaluate_closure(
        invoice_date=invoice_date,
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="59037075",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 30),
        min_age_days=6,
    )
    assert too_young_result["reasons"] == ["invoice_too_new"]


def test_invoice_target_mode_quarantines_duplicate_po_invoices(monkeypatch, capsys):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-08-01T08:00:00",
            "po_number": "FPO1086518",
            "vin": "3GNAXHEG4TL354008",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        },
        {
            "subject": "Invoice #2",
            "received": "2026-08-02T08:00:00",
            "po_number": "FPO1086518",
            "vin": "3GNAXHEG4TL354008",
            "invoice_amount": "340.00",
            "pdf_path": "two.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        },
    ]
    saved_rows = []
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: saved_rows.extend(updated))

    results = agn_invoices.step_check(
        po_numbers=["FPO1086518"],
        target_by_po={
            "FPO1086518": {
                "mva": "060091511",
                "vin": "3GNAXHEG4TL354008",
                "po_number": "FPO1086518",
            }
        },
    )

    assert results == []
    assert [row["status"] for row in rows] == ["identity_mismatch", "identity_mismatch"]
    assert "quarantining PO(s): FPO1086518" in capsys.readouterr().out


def test_check_rejects_unknown_po_status_before_approval(monkeypatch, capsys):
    row = {
        "subject": "Invoice #1",
        "received": "2026-07-01T08:00:00",
        "po_number": "FPO1091171",
        "vin": "5XYP6DGC5SG694797",
        "invoice_amount": "340.00",
        "pdf_path": "one.pdf",
        "status": "new",
        "auth_amount": "",
        "match": "",
    }
    context = MagicMock()
    page = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_displayed_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [row])
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "059037075",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page, po: True)
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page, po: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("UNKNOWN", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda rows: None)

    results = agn_invoices.step_check(max_invoices=1, approve_eligible=True)

    assert results[0]["check_status"] == "unexpected_po_status"
    assert row["status"] == "error"
    approve_displayed_po.assert_not_called()
    output = capsys.readouterr().out
    assert "FPO1091171 - SKIPPED (status_unknown)" in output
    assert "[PROGRESS] FINAL 1/1" in output


def test_direct_approval_requires_matching_mva_before_opening_po(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    click_into_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "099999999",
    )
    monkeypatch.setattr(agn_invoices, "click_into_po", click_into_po)
    monkeypatch.setattr(agn_invoices, "append_direct_approval_result", lambda record: None)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)

    results = agn_invoices.step_direct_approve([
        {"mva": "053791706", "vin": "KMHLM4DG4RU662228", "po_number": "FPO1059300"}
    ])

    assert results[0]["result"] == "skipped"
    assert "MVA mismatch" in results[0]["detail"]
    click_into_po.assert_not_called()


def test_direct_approval_selects_and_approves_exact_po(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_displayed_po = MagicMock(
        side_effect=lambda *args, **kwargs: kwargs["close_work_order"]
    )
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "053791706",
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page, po: True)
    click_into_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "click_into_po", click_into_po)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "150.00"),
    )
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "append_direct_approval_result", lambda record: None)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)

    results = agn_invoices.step_direct_approve([
        {"mva": "053791706", "vin": "KMHLM4DG4RU662228", "po_number": "FPO1059300"}
    ])

    assert results[0]["result"] == "approved_closed"
    click_into_po.assert_called_once_with(page, "FPO1059300")
    approve_displayed_po.assert_called_once_with(
        page,
        "KMHLM4DG4RU662228",
        request_permission=False,
        expected_po_number="FPO1059300",
        close_work_order=True,
        allow_open_if_closure_unavailable=True,
    )


def test_direct_approval_keeps_work_order_open_until_final_listed_po(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_displayed_po = MagicMock(
        side_effect=lambda *args, **kwargs: kwargs["close_work_order"]
    )
    targets = [
        {"mva": "057935872", "vin": "1V2WR2CA3SC573796", "po_number": "FPO1062203"},
        {"mva": "057935872", "vin": "1V2WR2CA3SC573796", "po_number": "FPO1062962"},
    ]
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "057935872",
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page, po: True)
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page, po: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "append_direct_approval_result", lambda record: None)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)

    results = agn_invoices.step_direct_approve(targets)

    assert [result["result"] for result in results] == ["approved_open", "approved_closed"]
    assert approve_displayed_po.call_args_list[0].kwargs["close_work_order"] is False
    assert approve_displayed_po.call_args_list[1].kwargs["close_work_order"] is True


def test_approval_stays_open_when_closure_checkbox_is_unavailable(monkeypatch):
    page = MagicMock()
    checkbox_section = MagicMock()
    filtered_section = MagicMock()
    close_work_order = MagicMock()
    close_work_order.count.return_value = 0
    success_message = MagicMock()
    success_message.count.return_value = 1
    success_message.inner_text.return_value = "PO# FPO1066268 approved and email sent to the supplier."
    page.get_by_role.return_value = MagicMock()
    page.locator.side_effect = [checkbox_section, success_message]
    checkbox_section.filter.return_value = filtered_section
    filtered_section.locator.return_value = close_work_order

    work_order_closed = agn_invoices.approve_displayed_po(
        page,
        "1FMDE7BH8SLA44744",
        request_permission=False,
        expected_po_number="FPO1066268",
        close_work_order=True,
        allow_open_if_closure_unavailable=True,
    )

    assert work_order_closed is False
    page.get_by_role.assert_any_call("button", name="Confirm", exact=True)


def test_direct_approval_start_index_skips_completed_prefix(monkeypatch):
    visited_vins = []
    page = MagicMock()
    context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    targets = [
        {"mva": "053791706", "vin": "KMHLM4DG4RU662228", "po_number": "FPO1059300"},
        {"mva": "054036953", "vin": "3FMTK3SU1PMA97379", "po_number": "FPO1059310"},
    ]
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: visited_vins.append(vin) or "054036953",
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page, po: True)
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page, po: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("APPROVED", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_direct_approval_result", lambda record: None)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)

    results = agn_invoices.step_direct_approve(targets, start_index=1)

    assert visited_vins == ["3FMTK3SU1PMA97379"]
    assert [result["po_number"] for result in results] == ["FPO1059310"]


def test_direct_approval_rejects_foreign_work_order_creator(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_displayed_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "060097365",
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Another User",
    )
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "append_direct_approval_result", lambda record: None)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)

    results = agn_invoices.step_direct_approve([
        {"mva": "060097365", "vin": "3GNAXHEG0TL363983", "po_number": "FPO1074035"}
    ])

    assert results[0]["result"] == "skipped"
    assert "Another User" in results[0]["detail"]
    approve_displayed_po.assert_not_called()


def test_extract_invoice_po_number_from_known_agn_pdf():
    pdf_path = Path(agn_invoices.BASE_DIR) / "pdfs" / "_External__Invoice__5010825_02F00000.pdf"

    assert agn_invoices.extract_invoice_po_number(pdf_path) == "FPO1059310"


def test_extract_subject_po_number_requires_new_vendor_format():
    assert agn_invoices._extract_subject_po_number(
        "[External] Invoice #5263569 (PO # FPO1123117)"
    ) == "FPO1123117"
    assert agn_invoices._extract_subject_po_number(
        "[External] Invoice #5263569"
    ) == ""


def test_build_subject_po_filter_uses_known_pos_not_invoice_number():
    assert agn_invoices._build_subject_po_filter([
        "FPO1123118",
        "FPO1123117",
    ]) == (
        "@SQL=(\"urn:schemas:httpmail:subject\" ci_phrasematch 'PO # FPO1123117' OR "
        "\"urn:schemas:httpmail:subject\" ci_phrasematch 'PO # FPO1123118')"
    )


def test_click_into_po_selects_exact_requested_po(monkeypatch):
    page = MagicMock()
    po_link = MagicMock()
    po_link.count.return_value = 1
    page.get_by_text.return_value = po_link
    monkeypatch.setattr(agn_invoices, "dismiss_attention_popup", lambda current_page: None)

    agn_invoices.click_into_po(page, "FPO1059310")

    page.get_by_text.assert_called_once_with("FPO1059310", exact=True)
    po_link.click.assert_called_once_with()
    page.locator.assert_not_called()


def test_default_run_uses_single_live_check_and_action_pass(monkeypatch):
    calls = []
    extracted_rows = [{"subject": "Invoice #current", "vin": "1FMDE7BH9TLA47847"}]
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(
        agn_invoices,
        "step_extract",
        lambda **kwargs: calls.append(("extract", kwargs)) or extracted_rows,
    )
    monkeypatch.setattr(agn_invoices, "step_check", lambda **kwargs: calls.append(("check", kwargs)))
    monkeypatch.setattr(agn_invoices, "step_approve", lambda **kwargs: calls.append(("approve", kwargs)))
    monkeypatch.setattr(sys, "argv", ["agn_invoices.py", "--silent", "--max-invoices", "1"])

    agn_invoices.main()

    assert calls == [
        ("extract", {"max_invoices": 1}),
        (
            "check",
            {
                "max_invoices": 1,
                "approve_eligible": True,
                "confirm_each": False,
                "selected_rows": extracted_rows,
            },
        ),
    ]


def test_confirm_each_enables_development_permission_prompt(monkeypatch):
    calls = []
    extracted_rows = [{"subject": "Invoice #current", "vin": "1FMDE7BH9TLA47847"}]
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(agn_invoices, "step_extract", lambda **kwargs: extracted_rows)
    monkeypatch.setattr(agn_invoices, "step_check", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["agn_invoices.py", "--silent", "--max-invoices", "1", "--confirm-each"],
    )

    agn_invoices.main()

    assert calls == [
        {
            "max_invoices": 1,
            "approve_eligible": True,
            "confirm_each": True,
            "selected_rows": extracted_rows,
        }
    ]


def test_dry_run_passes_po_file_allowlist_to_selected_stages(monkeypatch, tmp_path):
    calls = []
    po_file = tmp_path / "po_numbers.txt"
    po_file.write_text("FPO1059310\n", encoding="utf-8")
    extracted_rows = [{"po_number": "FPO1059310", "vin": "1FMDE7BH9TLA47847"}]
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(
        agn_invoices,
        "step_extract",
        lambda **kwargs: calls.append(("extract", kwargs)) or extracted_rows,
    )
    monkeypatch.setattr(
        agn_invoices,
        "step_check",
        lambda **kwargs: calls.append(("check", kwargs)) or [],
    )
    monkeypatch.setattr(agn_invoices, "step_dry_run_review", lambda rows: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agn_invoices.py", "--dry-run", "--po-file", str(po_file), "--max-invoices", "15"],
    )

    agn_invoices.main()

    assert calls == [
        ("extract", {"max_invoices": 15, "po_numbers": ["FPO1059310"]}),
        (
            "check",
            {
                "max_invoices": 15,
                "return_home_after_each": True,
                "selected_rows": extracted_rows,
                "po_numbers": ["FPO1059310"],
            },
        ),
    ]


@pytest.mark.parametrize("invalid_value", ["0", "-1", "not-a-number"])
def test_max_invoices_rejects_invalid_values(monkeypatch, invalid_value):
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agn_invoices.py", "--silent", "--max-invoices", invalid_value],
    )

    with pytest.raises(SystemExit) as exc_info:
        agn_invoices.main()

    assert exc_info.value.code == 2


def test_extract_only_does_not_run_later_stages(monkeypatch):
    calls = []
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(agn_invoices, "step_extract", lambda **kwargs: calls.append(("extract", kwargs)))
    monkeypatch.setattr(agn_invoices, "step_check", lambda **kwargs: calls.append(("check", kwargs)))
    monkeypatch.setattr(agn_invoices, "step_approve", lambda **kwargs: calls.append(("approve", kwargs)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["agn_invoices.py", "--silent", "--extract-only", "--max-invoices", "1"],
    )

    agn_invoices.main()

    assert calls == [("extract", {"max_invoices": 1})]


def test_fieldpo_browser_launches_maximized(monkeypatch):
    playwright = MagicMock()
    context = MagicMock()
    page = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = context
    context.new_page.return_value = page
    monkeypatch.setattr(agn_invoices, "dismiss_attention_popup", lambda current_page: None)

    returned_context, returned_page = agn_invoices.connect_to_fieldpo(playwright)

    playwright.chromium.launch_persistent_context.assert_called_once_with(
        agn_invoices.BROWSER_PROFILE_DIR,
        headless=False,
        args=["--start-maximized"],
        no_viewport=True,
    )
    page.goto.assert_called_once_with(agn_invoices.FIELDPO_URL)
    assert returned_context is context
    assert returned_page is page


def test_check_cap_leaves_additional_new_invoices_untouched(monkeypatch):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-08-01T08:00:00",
            "vin": "1FMDE7BH9TLA47847",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        },
        {
            "subject": "Invoice #2",
            "received": "2026-08-02T08:00:00",
            "vin": "1GKENKKSXTJ197778",
            "invoice_amount": "340.00",
            "pdf_path": "two.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        },
    ]
    visited_vins = []
    saved_rows = []
    browser_context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()

    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, MagicMock()),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda page, vin: visited_vins.append(vin),
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda page: False)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: saved_rows.extend(updated))

    agn_invoices.step_check(max_invoices=1)

    assert visited_vins == ["1FMDE7BH9TLA47847"]
    assert saved_rows[0]["status"] == "no_po"
    assert saved_rows[1]["status"] == "new"
    browser_context.close.assert_called_once_with()


def test_check_selection_ignores_older_pending_queue_row(monkeypatch):
    older = {
        "subject": "Invoice #older",
        "received": "2026-07-01T08:00:00",
        "vin": "1FMDE7BH9TLA47847",
        "invoice_amount": "340.00",
        "pdf_path": "older.pdf",
        "status": "new",
        "auth_amount": "",
        "match": "",
    }
    current = {
        "subject": "Invoice #current",
        "received": "2026-08-01T08:00:00",
        "vin": "1GKENKKSXTJ197778",
        "invoice_amount": "340.00",
        "pdf_path": "current.pdf",
        "status": "new",
        "auth_amount": "",
        "match": "",
    }
    rows = [older, current]
    visited_vins = []
    browser_context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, MagicMock()),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda page, vin: visited_vins.append(vin),
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda page: False)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: None)

    agn_invoices.step_check(max_invoices=1, selected_rows=[current])

    assert visited_vins == ["1GKENKKSXTJ197778"]
    assert older["status"] == "new"
    assert current["status"] == "no_po"
    browser_context.close.assert_called_once_with()


def test_live_check_approves_in_same_vin_navigation(monkeypatch):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-07-01T08:00:00",
            "vin": "1FMDE7BH9TLA47847",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        }
    ]
    browser_context = MagicMock()
    page = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    go_to_active_work_order_tab = MagicMock(return_value="056477761")
    approve_displayed_po = MagicMock(return_value=True)
    approve_one_vin = MagicMock()
    finalize_outlook_invoice = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, page),
    )
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", go_to_active_work_order_tab)
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page: True)
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Steele, Dirk")
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "evaluate_checked_invoice", lambda result: {"reasons": []})
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "approve_one_vin", approve_one_vin)
    monkeypatch.setattr(agn_invoices, "finalize_outlook_invoice", finalize_outlook_invoice)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: None)

    agn_invoices.step_check(
        max_invoices=1,
        approve_eligible=True,
        confirm_each=True,
    )

    go_to_active_work_order_tab.assert_called_once_with(page, "1FMDE7BH9TLA47847")
    approve_displayed_po.assert_called_once_with(
        page,
        "1FMDE7BH9TLA47847",
        request_permission=True,
    )
    approve_one_vin.assert_not_called()
    finalize_outlook_invoice.assert_called_once_with(rows[0])
    assert rows[0]["status"] == "approved"
    browser_context.close.assert_called_once_with()


def test_finalize_outlook_invoice_moves_and_logs_exact_entry(monkeypatch, capsys):
    namespace = MagicMock()
    mail = MagicMock()
    namespace.GetItemFromID.return_value = mail
    outlook_application = MagicMock()
    outlook_application.GetNamespace.return_value = namespace
    invoice_folder = MagicMock()
    processed_folder = MagicMock()
    monkeypatch.setattr(
        agn_invoices.win32com.client,
        "Dispatch",
        lambda application: outlook_application,
    )
    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: invoice_folder)
    monkeypatch.setattr(
        agn_invoices,
        "get_or_create_processed_folder",
        lambda folder: processed_folder,
    )

    agn_invoices.finalize_outlook_invoice({
        "_outlook_entry_id": "entry-green",
        "subject": "[External] Invoice #5244466",
    })

    namespace.GetItemFromID.assert_called_once_with("entry-green")
    mail.Move.assert_called_once_with(processed_folder)
    assert (
        "Outlook email moved to Processed: [External] Invoice #5244466"
        in capsys.readouterr().out
    )


def test_already_approved_invoice_moves_without_approval_action(monkeypatch, capsys):
    row = {
        "subject": "Invoice #green",
        "received": "2026-06-20T08:00:00",
        "vin": "1FMDE7BH9TLA47847",
        "invoice_amount": "340.00",
        "pdf_path": "green.pdf",
        "status": "new",
        "auth_amount": "",
        "match": "",
    }
    selected_row = {**row, "_outlook_entry_id": "entry-green"}
    browser_context = MagicMock()
    page = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    finalize_outlook_invoice = MagicMock()
    approve_displayed_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [row])
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "056477761",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page: True)
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Steele, Dirk")
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("APPROVED", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "finalize_outlook_invoice", finalize_outlook_invoice)
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: None)

    agn_invoices.step_check(max_invoices=1, approve_eligible=True, selected_rows=[selected_row])

    assert row["status"] == "already_paid"
    assert row["_outlook_entry_id"] == "entry-green"
    finalize_outlook_invoice.assert_called_once_with(row)
    approve_displayed_po.assert_not_called()
    browser_context.close.assert_called_once_with()
    output = capsys.readouterr().out
    assert "FieldPO status: already APPROVED. No approval action needed." in output
    assert "Continuing to next invoice." in output
    assert "skipping, moving to next invoice" not in output


def test_live_check_strict_gate_failure_never_calls_approval(monkeypatch):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-07-01T08:00:00",
            "vin": "1FMDE7BH9TLA47847",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        }
    ]
    browser_context = MagicMock()
    page = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_displayed_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, page),
    )
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "056477761",
    )
    monkeypatch.setattr(agn_invoices, "po_exists", lambda current_page: True)
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Another User")
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )
    monkeypatch.setattr(
        agn_invoices,
        "evaluate_checked_invoice",
        lambda result: {"reasons": ["work_order_created_by_mismatch"]},
    )
    monkeypatch.setattr(agn_invoices, "approve_displayed_po", approve_displayed_po)
    monkeypatch.setattr(agn_invoices, "return_to_fieldpo_home", lambda current_page: None)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: None)

    agn_invoices.step_check(max_invoices=1, approve_eligible=True)

    approve_displayed_po.assert_not_called()
    assert rows[0]["status"] == "checked"
    browser_context.close.assert_called_once_with()


@pytest.mark.parametrize(
    "vin",
    [
        "",
        "1234567890123456",
        "123456789012345678",
        "1FMDE7BI9TLA47847",
        "1FMDE7BO9TLA47847",
        "1FMDE7BQ9TLA47847",
    ],
)
def test_invalid_vins_are_rejected(vin):
    assert not agn_invoices.is_valid_vin(vin)


def test_invalid_queued_vin_skips_fieldpo_for_current_run(monkeypatch, capsys):
    rows = [
        {
            "subject": "Invoice #bad-vin",
            "received": "2026-08-01T08:00:00",
            "vin": "INVALID",
            "invoice_amount": "340.00",
            "pdf_path": "bad.pdf",
            "status": "new",
            "auth_amount": "",
            "match": "",
        }
    ]
    connect_to_fieldpo = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "connect_to_fieldpo", connect_to_fieldpo)

    agn_invoices.step_check(max_invoices=1)

    assert rows[0]["status"] == "new"
    connect_to_fieldpo.assert_not_called()
    assert "invalid VIN: INVALID" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("raw_mva", "expected"),
    [
        ("56477761", "056477761"),
        ("056477761", "056477761"),
        ("", ""),
        ("1234567", ""),
        ("1234567890", ""),
        ("12A45678", ""),
    ],
)
def test_normalize_mva_rejects_invalid_values(raw_mva, expected):
    assert agn_invoices.normalize_mva(raw_mva) == expected


def _mail_folder_with(*mail):
    items = MagicMock()
    items.__iter__.return_value = iter(mail)
    folder = MagicMock()
    folder.Items = items
    return folder


def _invoice_mail(subject, received, entry_id):
    attachment = MagicMock()
    attachment.FileName = "invoice.pdf"
    mail = MagicMock()
    mail.Class = 43
    mail.Subject = subject
    mail.Categories = ""
    mail.EntryID = entry_id
    mail.ReceivedTime = received
    mail.Attachments = [attachment]
    return mail


def test_extract_cap_selects_oldest_invoice_first(monkeypatch):
    newer = _invoice_mail("Invoice #newer", "2026-08-02T08:00:00", "entry-newer")
    older = _invoice_mail("Invoice #older", "2026-08-01T08:00:00", "entry-older")
    queued = []

    monkeypatch.setattr(
        agn_invoices,
        "get_invoice_folder",
        lambda: _mail_folder_with(newer, older),
    )
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(
        agn_invoices,
        "extract_vin_and_amount",
        lambda path: ("1FMDE7BH9TLA47847", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_to_queue", queued.extend)

    extracted_rows = agn_invoices.step_extract(max_invoices=1)

    assert [row["subject"] for row in queued] == ["Invoice #older"]
    assert extracted_rows == queued
    older.Attachments[0].SaveAsFile.assert_called_once()
    newer.Attachments[0].SaveAsFile.assert_not_called()


def test_targeted_extract_prefilters_exact_subject_po_before_download(monkeypatch):
    matching = _invoice_mail(
        "[External] Invoice #5263569 (PO # FPO1123117)",
        "2026-09-03T08:00:00",
        "entry-matching",
    )
    unrelated = _invoice_mail(
        "[External] Invoice #5263570 (PO # FPO1123118)",
        "2026-09-03T07:00:00",
        "entry-unrelated",
    )
    invoice_folder = MagicMock()
    candidate_folder = _mail_folder_with(unrelated, matching)
    candidate_folder.Items.Restrict.return_value = [unrelated, matching]
    queued = []
    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: invoice_folder)
    monkeypatch.setattr(
        agn_invoices,
        "iter_outlook_folders",
        lambda folder: [(candidate_folder, "AGN\\Invoice")],
    )
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(
        agn_invoices,
        "extract_vin_and_amount",
        lambda path: ("1FMDE7BH9TLA47847", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_to_queue", queued.extend)

    extracted_rows = agn_invoices.step_extract(
        max_invoices=1,
        po_numbers=["FPO1123117"],
    )

    assert extracted_rows[0]["po_number"] == "FPO1123117"
    candidate_folder.Items.Restrict.assert_called_once_with(
        "@SQL=(\"urn:schemas:httpmail:subject\" ci_phrasematch 'PO # FPO1123117')"
    )
    matching.Attachments[0].SaveAsFile.assert_called_once()
    unrelated.Attachments[0].SaveAsFile.assert_not_called()


def test_extract_limit_above_available_processes_all_invoices(monkeypatch, capsys):
    first = _invoice_mail("Invoice #first", "2026-08-01T08:00:00", "entry-first")
    second = _invoice_mail("Invoice #second", "2026-08-02T08:00:00", "entry-second")
    queued = []
    vins = iter(["1FMDE7BH9TLA47847", "1GKENKKSXTJ197778"])
    monkeypatch.setattr(
        agn_invoices,
        "get_invoice_folder",
        lambda: _mail_folder_with(first, second),
    )
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(
        agn_invoices,
        "extract_vin_and_amount",
        lambda path: (next(vins), "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_to_queue", queued.extend)

    extracted_rows = agn_invoices.step_extract(max_invoices=50)

    assert [row["subject"] for row in extracted_rows] == [
        "Invoice #first",
        "Invoice #second",
    ]
    assert extracted_rows == queued
    assert "Reached invoice limit" not in capsys.readouterr().out


def test_categorized_invoice_is_ignored_when_backlog_mode_disabled(monkeypatch):
    mail = _invoice_mail("Invoice #green", "2026-06-20T08:00:00", "entry-green")
    mail.Categories = "Green Category"
    extract_data = MagicMock()
    monkeypatch.setattr(agn_invoices, "PROCESS_CATEGORIZED_INVOICES", False)
    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "extract_vin_and_amount", extract_data)

    extracted_rows = agn_invoices.step_extract(max_invoices=1)

    assert extracted_rows == []
    extract_data.assert_not_called()
    mail.Move.assert_not_called()


def test_uncategorized_invoice_precedes_older_categorized_backlog(monkeypatch):
    categorized = _invoice_mail("Invoice #green", "2026-06-20T08:00:00", "entry-green")
    categorized.Categories = "Green Category"
    uncategorized = _invoice_mail("Invoice #new", "2026-08-25T08:00:00", "entry-new")
    queued = []

    monkeypatch.setattr(agn_invoices, "PROCESS_CATEGORIZED_INVOICES", True)
    monkeypatch.setattr(
        agn_invoices,
        "get_invoice_folder",
        lambda: _mail_folder_with(categorized, uncategorized),
    )
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(
        agn_invoices,
        "extract_vin_and_amount",
        lambda path: ("1FMDE7BH9TLA47847", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_to_queue", queued.extend)

    extracted_rows = agn_invoices.step_extract(max_invoices=1)

    assert [row["subject"] for row in extracted_rows] == ["Invoice #new"]
    assert extracted_rows == queued
    uncategorized.Attachments[0].SaveAsFile.assert_called_once()
    categorized.Attachments[0].SaveAsFile.assert_not_called()


def test_categorized_existing_invoice_is_requeued_without_moving_early(monkeypatch):
    mail = _invoice_mail("Invoice #green", "2026-06-20T08:00:00", "entry-green")
    mail.Categories = "Green Category"
    existing = {
        "subject": "Invoice #green",
        "received": "2026-06-20T08:00:00",
        "vin": "1FMDE7BH9TLA47847",
        "invoice_amount": "340.00",
        "pdf_path": "old.pdf",
        "status": "approved",
        "auth_amount": "340.00",
        "match": "True",
    }
    saved_rows = []
    monkeypatch.setattr(agn_invoices, "PROCESS_CATEGORIZED_INVOICES", True)
    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [existing])
    monkeypatch.setattr(
        agn_invoices,
        "extract_vin_and_amount",
        lambda path: ("1FMDE7BH9TLA47847", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "write_queue", lambda rows: saved_rows.extend(rows))

    extracted_rows = agn_invoices.step_extract(max_invoices=1)

    assert extracted_rows[0]["status"] == "new"
    assert extracted_rows[0]["_outlook_entry_id"] == "entry-green"
    assert saved_rows[0]["status"] == "new"
    mail.Move.assert_not_called()


def test_email_without_pdf_is_left_unprocessed(monkeypatch, capsys):
    mail = MagicMock()
    mail.Class = 43
    mail.Subject = "AGN status update"
    mail.Categories = ""
    mail.Attachments = []
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    append_to_queue.assert_not_called()
    assert "no PDF attachment" in capsys.readouterr().out


def test_receipt_email_is_reserved_for_close_queue(monkeypatch):
    mail = _invoice_mail("Receipt for Job #4946751", "2026-08-01T08:00:00", "receipt-entry")
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    mail.Attachments[0].SaveAsFile.assert_not_called()
    append_to_queue.assert_not_called()


def test_extract_receipt_vin_and_final_total(monkeypatch):
    page = MagicMock()
    page.extract_text.return_value = """
2026-06-25
Job #4963741
Vehicle Information
VIN KNDNB5KA2T6127711
Subtotal $387.15
Total $387.15
"""
    pdf = MagicMock()
    pdf.pages = [page]
    context = MagicMock()
    context.__enter__.return_value = pdf
    monkeypatch.setattr(agn_invoices.pdfplumber, "open", lambda path: context)

    vin, amount = agn_invoices.extract_receipt_vin_and_amount("receipt.pdf")

    assert vin == "KNDNB5KA2T6127711"
    assert amount == "387.15"


def test_pdf_without_invoice_fields_is_left_unprocessed(monkeypatch, capsys):
    attachment = MagicMock()
    attachment.FileName = "document.pdf"
    mail = MagicMock()
    mail.Class = 43
    mail.Subject = "AGN document"
    mail.Categories = ""
    mail.EntryID = "entry-12345678"
    mail.Attachments = [attachment]
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "extract_vin_and_amount", lambda path: (None, None))
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    append_to_queue.assert_not_called()
    assert "Could not parse" in capsys.readouterr().out


def test_closure_is_eligible_at_exactly_fourteen_days_with_matching_price():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 26),
        invoice_amount="340.00",
        auth_amount="$340.00",
        mva="56477761",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 9),
    )

    assert result == {
        "decision": "WOULD_CLOSE",
        "reasons": [],
        "mva": "056477761",
        "invoice_age_days": 14,
        "price_match": True,
    }


def test_closure_skips_invoice_younger_than_fourteen_days():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 27),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="056477761",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 9),
        min_age_days=14,
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["invoice_too_new"]


def test_closure_skips_old_invoice_with_price_mismatch():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="339.99",
        mva="056477761",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["price_mismatch"]


def test_closure_allows_price_mismatch_when_explicitly_excluded():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 24),
        invoice_amount="404.82",
        auth_amount="340.00",
        mva="58552222",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 30),
        allow_price_mismatch=True,
    )

    assert result["decision"] == "WOULD_CLOSE"
    assert result["reasons"] == []
    assert result["price_match"] is False


def test_normalize_invoice_sku_removes_confirmed_n_suffix():
    assert agn_invoices.normalize_invoice_sku("DW03055GTYN") == "DW03055GTY"
    assert agn_invoices.normalize_invoice_sku("DW03055GTY") == "DW03055GTY"


def test_checked_excluded_sku_uses_invoice_controlled_price(monkeypatch):
    monkeypatch.setattr(
        agn_invoices,
        "extract_invoice_data",
        lambda path: (date(2026, 7, 24), "1FMDE7BHXSLA56734", "404.82"),
    )
    monkeypatch.setattr(
        agn_invoices,
        "extract_invoice_sku",
        lambda path: "DW03055GTY",
    )

    result = agn_invoices.evaluate_checked_invoice({
        "pdf_path": "bronco.pdf",
        "vin": "1FMDE7BHXSLA56734",
        "invoice_amount": "404.82",
        "auth_amount": "340.00",
        "mva": "58552222",
        "work_order_created_by": "Steele, Dirk",
        "check_status": "checked",
    })

    assert result["excluded_price_sku"] is True
    assert result["invoice_sku"] == "DW03055GTY"
    assert result["reasons"] == []


def test_closure_skips_invalid_mva_even_when_other_gates_pass():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="INVALID",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["invalid_or_missing_mva"]


def test_closure_logs_all_missing_data_reasons():
    result = agn_invoices.evaluate_closure(
        invoice_date=None,
        invoice_amount="",
        auth_amount="",
        mva="",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == [
        "invalid_or_missing_mva",
        "missing_work_order_created_by",
        "missing_invoice_date",
        "invalid_or_missing_price",
    ]


def test_closure_accepts_configured_work_order_creator():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="056477761",
        work_order_created_by="Steele, Dirk",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "WOULD_CLOSE"
    assert result["reasons"] == []


def test_closure_rejects_reordered_work_order_creator_name():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="056477761",
        work_order_created_by="Dirk Steele",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["work_order_created_by_mismatch"]


def test_closure_skips_work_order_created_by_mismatch():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="056477761",
        work_order_created_by="Another User",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["work_order_created_by_mismatch"]


def test_closure_skips_missing_work_order_created_by():
    result = agn_invoices.evaluate_closure(
        invoice_date=date(2026, 7, 1),
        invoice_amount="340.00",
        auth_amount="340.00",
        mva="056477761",
        work_order_created_by="",
        system_date=date(2026, 8, 9),
    )

    assert result["decision"] == "SKIPPED"
    assert result["reasons"] == ["missing_work_order_created_by"]


def test_creator_gate_fails_closed_without_configured_creator(monkeypatch):
    monkeypatch.setattr(agn_invoices, "ALLOWED_WORK_ORDER_CREATED_BY", [])

    assert not agn_invoices.is_allowed_work_order_creator("Steele, Dirk")


def test_reads_work_order_created_by_from_verified_data_section():
    page = MagicMock()
    value = MagicMock()
    value.inner_text.return_value = "Steele, Dirk"
    page.locator.return_value = value

    result = agn_invoices.read_work_order_created_by(page)

    assert result == "Steele, Dirk"
    page.locator.assert_called_once_with(
        "div.dataSection > div:has(> span:text-is('Created By:')) > span:nth-child(2)"
    )


def _approval_row(**overrides):
    row = {
        "subject": "Invoice #1",
        "received": "2026-07-01T08:00:00",
        "vin": "1FMDE7BH9TLA47847",
        "invoice_amount": "340.00",
        "pdf_path": "one.pdf",
        "status": "checked",
        "auth_amount": "340.00",
        "match": "True",
    }
    row.update(overrides)
    return row


def test_approval_skips_before_po_when_work_order_creator_mismatches(monkeypatch):
    page = MagicMock()
    click_into_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", lambda current_page, vin: "056477761")
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Another User")
    monkeypatch.setattr(
        agn_invoices,
        "evaluate_checked_invoice",
        lambda result: {"reasons": ["work_order_created_by_mismatch"]},
    )
    monkeypatch.setattr(agn_invoices, "click_into_po", click_into_po)

    should_approve, creator, reasons = agn_invoices.approve_one_vin(
        page, _approval_row(), request_permission=False
    )

    assert not should_approve
    assert creator == "Another User"
    assert reasons == ["work_order_created_by_mismatch"]
    click_into_po.assert_not_called()


def test_approval_decline_holds_visible_po_and_does_not_click_approve(monkeypatch):
    page = MagicMock()
    events = []
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: events.append("work_order_visible"),
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(
        agn_invoices,
        "evaluate_checked_invoice",
        lambda result: {"reasons": []},
    )
    monkeypatch.setattr(
        agn_invoices,
        "click_into_po",
        lambda current_page: events.append("po_visible"),
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: events.append("permission_requested") or "no",
    )

    should_approve, creator, reasons = agn_invoices.approve_one_vin(
        page, _approval_row()
    )

    assert should_approve is None
    assert creator == "Steele, Dirk"
    assert reasons == ["user_declined"]
    assert events == ["work_order_visible", "po_visible", "permission_requested"]
    page.get_by_role.assert_not_called()


def test_approval_returns_already_approved_without_clicking_approve(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(
        agn_invoices,
        "go_to_active_work_order_tab",
        lambda current_page, vin: "056477761",
    )
    monkeypatch.setattr(
        agn_invoices,
        "read_work_order_created_by",
        lambda current_page: "Steele, Dirk",
    )
    monkeypatch.setattr(
        agn_invoices,
        "evaluate_checked_invoice",
        lambda result: {"reasons": []},
    )
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("APPROVED", "340.00"),
    )

    result = agn_invoices.approve_one_vin(
        page, _approval_row(), request_permission=False
    )

    assert result == (False, "Steele, Dirk", ["already_approved"])
    page.get_by_role.assert_not_called()


def test_approval_checks_close_work_order_before_confirming(monkeypatch):
    page = MagicMock()
    approve_button = MagicMock()
    checkbox_section = MagicMock()
    filtered_section = MagicMock()
    close_work_order = MagicMock()
    close_work_order.count.return_value = 1
    close_work_order.is_checked.side_effect = [False, True]
    confirm_button = MagicMock()
    success_message = MagicMock()
    success_message.count.return_value = 1
    success_message.inner_text.return_value = (
        "done PO# FPO1080762 approved and email sent to the supplier."
        "WO# WO751951 is closed. X"
    )
    page.get_by_role.side_effect = [approve_button, confirm_button]
    page.locator.side_effect = [checkbox_section, success_message]
    checkbox_section.filter.return_value = filtered_section
    filtered_section.locator.return_value = close_work_order
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", lambda current_page, vin: "056477761")
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Steele, Dirk")
    monkeypatch.setattr(agn_invoices, "evaluate_checked_invoice", lambda result: {"reasons": []})
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )

    should_approve, creator, reasons = agn_invoices.approve_one_vin(
        page, _approval_row(), request_permission=False
    )

    assert should_approve
    assert creator == "Steele, Dirk"
    assert reasons == []
    close_work_order.check.assert_called_once_with()
    confirm_button.click.assert_called_once_with()
    assert page.get_by_role.call_args_list == [
        (("button",), {"name": "Approve", "exact": True}),
        (("button",), {"name": "Confirm", "exact": True}),
    ]
    assert page.locator.call_args_list == [
        (("div.checkboxSection",), {}),
        (("div.successMsg",), {}),
    ]
    filtered_section.locator.assert_called_once_with("input.checkboxInput[type='checkbox']")
    success_message.wait_for.assert_called_once_with(state="visible", timeout=30000)


def test_approval_fails_when_success_message_does_not_confirm_closure(monkeypatch):
    page = MagicMock()
    checkbox_section = MagicMock()
    filtered_section = MagicMock()
    close_work_order = MagicMock()
    close_work_order.count.return_value = 1
    close_work_order.is_checked.return_value = True
    success_message = MagicMock()
    success_message.count.return_value = 1
    success_message.inner_text.return_value = "PO# FPO1080762 approved."
    page.get_by_role.return_value = MagicMock()
    page.locator.side_effect = [checkbox_section, success_message]
    checkbox_section.filter.return_value = filtered_section
    filtered_section.locator.return_value = close_work_order
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", lambda current_page, vin: "056477761")
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Steele, Dirk")
    monkeypatch.setattr(agn_invoices, "evaluate_checked_invoice", lambda result: {"reasons": []})
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )

    with pytest.raises(RuntimeError, match="Work Order closure was not confirmed"):
        agn_invoices.approve_one_vin(
            page, _approval_row(), request_permission=False
        )


def test_approval_failure_is_persisted_and_stops_run(monkeypatch):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-07-01T08:00:00",
            "vin": "1FMDE7BH9TLA47847",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "checked",
            "auth_amount": "340.00",
            "match": "True",
        },
        {
            "subject": "Invoice #2",
            "received": "2026-07-02T08:00:00",
            "vin": "1GKENKKSXTJ197778",
            "invoice_amount": "340.00",
            "pdf_path": "two.pdf",
            "status": "checked",
            "auth_amount": "340.00",
            "match": "True",
        },
    ]
    browser_context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    approve_one_vin = MagicMock(side_effect=RuntimeError("closure was not confirmed"))
    saved_rows = []
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, MagicMock()),
    )
    monkeypatch.setattr(agn_invoices, "approve_one_vin", approve_one_vin)
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: saved_rows.extend(updated))

    with pytest.raises(RuntimeError, match="Approval run stopped"):
        agn_invoices.step_approve(confirm_each=False, max_invoices=2)

    assert approve_one_vin.call_count == 1
    assert saved_rows[0]["status"] == "approve_failed"
    assert saved_rows[1]["status"] == "checked"
    browser_context.close.assert_called_once_with()


def test_successful_approval_is_persisted(monkeypatch):
    rows = [
        {
            "subject": "Invoice #1",
            "received": "2026-07-01T08:00:00",
            "vin": "1FMDE7BH9TLA47847",
            "invoice_amount": "340.00",
            "pdf_path": "one.pdf",
            "status": "checked",
            "auth_amount": "340.00",
            "match": "True",
        }
    ]
    browser_context = MagicMock()
    playwright_context = MagicMock()
    playwright_context.__enter__.return_value = object()
    saved_rows = []
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "sync_playwright", lambda: playwright_context)
    monkeypatch.setattr(
        agn_invoices,
        "connect_to_fieldpo",
        lambda playwright: (browser_context, MagicMock()),
    )
    monkeypatch.setattr(
        agn_invoices,
        "approve_one_vin",
        lambda page, row, request_permission: (True, "Steele, Dirk", []),
    )
    monkeypatch.setattr(agn_invoices, "write_queue", lambda updated: saved_rows.extend(updated))

    agn_invoices.step_approve(confirm_each=False, max_invoices=1)

    assert saved_rows[0]["status"] == "approved"
    browser_context.close.assert_called_once_with()


def test_old_price_mismatch_never_reaches_live_approval(monkeypatch, capsys):
    rows = [_approval_row(auth_amount="300.00", match="False")]
    connect_to_fieldpo = MagicMock()
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: rows)
    monkeypatch.setattr(agn_invoices, "connect_to_fieldpo", connect_to_fieldpo)

    agn_invoices.step_approve(confirm_each=False, max_invoices=1)

    connect_to_fieldpo.assert_not_called()
    assert rows[0]["status"] == "checked"
    assert "NEEDS MANUAL REVIEW" in capsys.readouterr().out


def test_approval_does_not_confirm_without_close_work_order_checkbox(monkeypatch):
    page = MagicMock()
    approve_button = MagicMock()
    checkbox_section = MagicMock()
    filtered_section = MagicMock()
    close_work_order = MagicMock()
    close_work_order.count.return_value = 0
    page.get_by_role.return_value = approve_button
    page.locator.return_value = checkbox_section
    checkbox_section.filter.return_value = filtered_section
    filtered_section.locator.return_value = close_work_order
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", lambda current_page, vin: "056477761")
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Steele, Dirk")
    monkeypatch.setattr(agn_invoices, "evaluate_checked_invoice", lambda result: {"reasons": []})
    monkeypatch.setattr(agn_invoices, "click_into_po", lambda current_page: None)
    monkeypatch.setattr(
        agn_invoices,
        "read_po_status_and_amount",
        lambda current_page: ("IN PROGRESS", "340.00"),
    )

    with pytest.raises(RuntimeError, match="Approval was not confirmed"):
        agn_invoices.approve_one_vin(
            page, _approval_row(), request_permission=False
        )

    page.get_by_role.assert_called_once_with("button", name="Approve", exact=True)


def test_dry_run_never_invokes_approval(monkeypatch):
    check_results = [{"vin": "1FMDE7BH9TLA47847", "check_status": "checked"}]
    step_extract = MagicMock()
    extracted_rows = [{"subject": "Invoice #current", "vin": "1FMDE7BH9TLA47847"}]
    step_extract.return_value = extracted_rows
    step_check = MagicMock(return_value=check_results)
    step_dry_run_review = MagicMock()
    step_approve = MagicMock()
    approve_one_vin = MagicMock()
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(agn_invoices, "step_extract", step_extract)
    monkeypatch.setattr(agn_invoices, "step_check", step_check)
    monkeypatch.setattr(agn_invoices, "step_dry_run_review", step_dry_run_review)
    monkeypatch.setattr(agn_invoices, "step_approve", step_approve)
    monkeypatch.setattr(agn_invoices, "approve_one_vin", approve_one_vin)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agn_invoices.py", "--silent", "--dry-run", "--max-invoices", "1"],
    )

    agn_invoices.main()

    step_extract.assert_called_once_with(max_invoices=1)
    step_check.assert_called_once_with(
        max_invoices=1,
        return_home_after_each=True,
        selected_rows=extracted_rows,
    )
    step_dry_run_review.assert_called_once_with(check_results)
    step_approve.assert_not_called()
    approve_one_vin.assert_not_called()


def test_return_to_fieldpo_home_uses_verified_icon(monkeypatch):
    page = MagicMock()
    home_icon = MagicMock()
    page.locator.return_value = home_icon
    monkeypatch.setattr(agn_invoices, "dismiss_attention_popup", lambda current_page: None)

    agn_invoices.return_to_fieldpo_home(page)

    page.locator.assert_called_once_with("mat-icon[aria-label='home']")
    home_icon.click.assert_called_once_with()
    page.wait_for_url.assert_called_once_with("**/fieldpo/dashboard**", timeout=30000)


def test_dry_run_logs_would_close_without_side_effects(monkeypatch):
    logged = []
    invoice_date = date.today() - timedelta(days=14)
    monkeypatch.setattr(
        agn_invoices,
        "extract_invoice_data",
        lambda path: (invoice_date, "1FMDE7BH9TLA47847", "340.00"),
    )
    monkeypatch.setattr(agn_invoices, "append_closure_decision", logged.append)

    decisions = agn_invoices.step_dry_run_review(
        [
            {
                "subject": "Invoice #1",
                "vin": "1FMDE7BH9TLA47847",
                "mva": "56477761",
                "invoice_amount": "340.00",
                "auth_amount": "340.00",
                "work_order_created_by": "Steele, Dirk",
                "pdf_path": "invoice.pdf",
                "check_status": "checked",
            }
        ]
    )

    assert decisions[0]["decision"] == "WOULD_CLOSE"
    assert decisions[0]["reasons"] == []
    assert logged == decisions


def test_dry_run_logs_missing_pdf_as_skipped(monkeypatch):
    logged = []
    monkeypatch.setattr(
        agn_invoices,
        "extract_invoice_data",
        MagicMock(side_effect=FileNotFoundError("missing.pdf")),
    )
    monkeypatch.setattr(agn_invoices, "append_closure_decision", logged.append)

    decisions = agn_invoices.step_dry_run_review(
        [
            {
                "subject": "Invoice #missing",
                "vin": "1FMDE7BH9TLA47847",
                "mva": "56477761",
                "invoice_amount": "340.00",
                "auth_amount": "340.00",
                "pdf_path": "missing.pdf",
                "check_status": "checked",
            }
        ]
    )

    assert decisions[0]["decision"] == "SKIPPED"
    assert "invoice_pdf_unavailable" in decisions[0]["reasons"]
    assert "missing_invoice_date" in decisions[0]["reasons"]
    assert logged == decisions