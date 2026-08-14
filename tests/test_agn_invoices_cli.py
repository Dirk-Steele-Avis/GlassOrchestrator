"""Tests for the AGN invoice command-line interface."""

import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from outlook import agn_invoices


def test_max_invoices_is_forwarded_to_all_stages(monkeypatch):
    calls = []
    monkeypatch.setattr(agn_invoices, "setup_logging", lambda: None)
    monkeypatch.setattr(agn_invoices, "step_extract", lambda **kwargs: calls.append(("extract", kwargs)))
    monkeypatch.setattr(agn_invoices, "step_check", lambda **kwargs: calls.append(("check", kwargs)))
    monkeypatch.setattr(agn_invoices, "step_approve", lambda **kwargs: calls.append(("approve", kwargs)))
    monkeypatch.setattr(sys, "argv", ["agn_invoices.py", "--silent", "--max-invoices", "1"])

    agn_invoices.main()

    assert calls == [
        ("extract", {"max_invoices": 1}),
        ("check", {"max_invoices": 1}),
        ("approve", {"auto_confirm": False, "max_invoices": 1}),
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
    monkeypatch.setattr(agn_invoices, "mark_processed_category", lambda mail: None)
    monkeypatch.setattr(agn_invoices, "append_to_queue", queued.extend)

    agn_invoices.step_extract(max_invoices=1)

    assert [row["subject"] for row in queued] == ["Invoice #older"]
    older.Attachments[0].SaveAsFile.assert_called_once()
    newer.Attachments[0].SaveAsFile.assert_not_called()


def test_email_without_pdf_is_left_unprocessed(monkeypatch, capsys):
    mail = MagicMock()
    mail.Class = 43
    mail.Subject = "AGN status update"
    mail.Categories = ""
    mail.Attachments = []
    mark_processed = MagicMock()
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "mark_processed_category", mark_processed)
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    mark_processed.assert_not_called()
    append_to_queue.assert_not_called()
    assert "no PDF attachment" in capsys.readouterr().out


def test_receipt_email_is_reserved_for_close_queue(monkeypatch):
    mail = _invoice_mail("Receipt for Job #4946751", "2026-08-01T08:00:00", "receipt-entry")
    mark_processed = MagicMock()
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "mark_processed_category", mark_processed)
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    mail.Attachments[0].SaveAsFile.assert_not_called()
    mark_processed.assert_not_called()
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
    mark_processed = MagicMock()
    append_to_queue = MagicMock()

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: _mail_folder_with(mail))
    monkeypatch.setattr(agn_invoices, "read_queue", lambda: [])
    monkeypatch.setattr(agn_invoices, "extract_vin_and_amount", lambda path: (None, None))
    monkeypatch.setattr(agn_invoices, "mark_processed_category", mark_processed)
    monkeypatch.setattr(agn_invoices, "append_to_queue", append_to_queue)

    agn_invoices.step_extract(max_invoices=1)

    mark_processed.assert_not_called()
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


def test_approval_skips_before_po_when_work_order_creator_mismatches(monkeypatch):
    page = MagicMock()
    click_into_po = MagicMock()
    monkeypatch.setattr(agn_invoices, "go_to_active_work_order_tab", lambda current_page, vin: "056477761")
    monkeypatch.setattr(agn_invoices, "read_work_order_created_by", lambda current_page: "Another User")
    monkeypatch.setattr(agn_invoices, "click_into_po", click_into_po)

    should_approve, creator = agn_invoices.approve_one_vin(page, "1FMDE7BH9TLA47847")

    assert not should_approve
    assert creator == "Another User"
    click_into_po.assert_not_called()


def test_dry_run_never_invokes_approval(monkeypatch):
    check_results = [{"vin": "1FMDE7BH9TLA47847", "check_status": "checked"}]
    step_extract = MagicMock()
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
    step_check.assert_called_once_with(max_invoices=1, return_home_after_each=True)
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