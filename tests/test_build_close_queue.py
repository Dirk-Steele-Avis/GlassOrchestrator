from pathlib import Path
from unittest.mock import MagicMock, call

from WorkItems.build_close_queue import (
    _append_candidates,
    _load_existing_candidates,
    _load_uncategorized_invoices,
    _retire_processed_candidates,
    build_close_candidates,
)


def test_apply_ensures_vendor_columns_before_processing(monkeypatch, tmp_path):
    import WorkItems.build_close_queue as build_close_queue

    updater = MagicMock()
    monkeypatch.setattr(build_close_queue, "VendorSheetUpdater", MagicMock(return_value=updater))
    monkeypatch.setattr(build_close_queue, "_load_config", lambda: {"spreadsheet_id": "sheet-id"})
    monkeypatch.setattr(build_close_queue, "_load_uncategorized_invoices", lambda: ([], []))
    monkeypatch.setattr(build_close_queue, "_load_existing_candidates", lambda _path: set())
    monkeypatch.setattr(build_close_queue, "_load_processed_candidates", lambda _path: set())
    monkeypatch.setattr(build_close_queue, "_retire_processed_candidates", lambda *_args: 0)
    monkeypatch.setattr(build_close_queue, "_append_candidates", lambda *_args: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_close_queue.py",
            "--apply",
            "--close-queue",
            str(tmp_path / "close.csv"),
            "--history",
            str(tmp_path / "history.csv"),
        ],
    )

    assert build_close_queue.main() == 0
    assert updater.method_calls[:2] == [call.connect(), call.ensure_columns()]


def test_apply_marks_processed_mail_after_successful_update(monkeypatch, tmp_path):
    import WorkItems.build_close_queue as build_close_queue

    updater = MagicMock()
    updater.method_calls = []
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.return_value = {
        "MVA": "12345678",
        "Inventory Date": "8/17/2026",
    }

    mail = MagicMock()
    mail.Categories = ""

    monkeypatch.setattr(build_close_queue, "VendorSheetUpdater", MagicMock(return_value=updater))
    monkeypatch.setattr(build_close_queue, "_load_config", lambda: {"spreadsheet_id": "sheet-id"})
    monkeypatch.setattr(
        build_close_queue,
        "_load_uncategorized_invoices",
        lambda: ([{"subject": "Invoice #201", "vin": "VIN1", "received": "2026-02-01", "invoice_amount": "125.00", "mail": mail}], []),
    )
    monkeypatch.setattr(build_close_queue, "_load_existing_candidates", lambda _path: set())
    monkeypatch.setattr(build_close_queue, "_load_processed_candidates", lambda _path: set())
    monkeypatch.setattr(build_close_queue, "_retire_processed_candidates", lambda *_args: 0)
    monkeypatch.setattr(build_close_queue, "_append_candidates", lambda *_args: None)
    mark_mock = MagicMock(return_value=True)
    monkeypatch.setattr(build_close_queue, "mark_processed_category", mark_mock)
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_close_queue.py",
            "--apply",
            "--close-queue",
            str(tmp_path / "close.csv"),
            "--history",
            str(tmp_path / "history.csv"),
        ],
    )

    assert build_close_queue.main() == 0
    mark_mock.assert_called_once_with(mail)


def test_loads_only_uncategorized_outlook_invoices_oldest_first(monkeypatch):
    import outlook.agn_invoices as agn_invoices

    def make_mail(subject, received, categorized=False):
        attachment = MagicMock()
        attachment.FileName = "invoice.pdf"
        mail = MagicMock()
        mail.Class = 43
        mail.Subject = subject
        mail.ReceivedTime = received
        mail.Categories = "Green Category" if categorized else ""
        mail.Attachments = [attachment]
        return mail

    newer = make_mail("Invoice #202", "2026-02-02T08:00:00")
    older = make_mail("Invoice #201", "2026-02-01T08:00:00")
    categorized = make_mail("Invoice #200", "2026-01-01T08:00:00", categorized=True)
    receipt = make_mail("Receipt for Job #100", "2026-01-02T08:00:00")
    folder = MagicMock()
    folder.Items = [newer, categorized, receipt, older]

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: folder)
    monkeypatch.setattr(
        agn_invoices,
        "extract_invoice_data",
        lambda path: (None, "1HGBH41JXMN109186", "306.00"),
    )

    invoices, review_notes = _load_uncategorized_invoices()

    assert [row["subject"] for row in invoices] == [
        "Invoice #201",
        "Invoice #202",
    ]
    assert review_notes == []
    categorized.Attachments[0].SaveAsFile.assert_not_called()
    receipt.Attachments[0].SaveAsFile.assert_not_called()


def test_same_mva_duplicate_vin_rows_select_latest_inventory_date():
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4, 9]
    updater.get_row_fields.side_effect = [
        {"MVA": "12345678", "Inventory Date": "8/16/2026"},
        {"MVA": "12345678", "Inventory Date": "8/17/2026"},
    ]
    invoices = [
        {"subject": "Invoice #201", "vin": "VIN1", "received": "2026-02-01", "invoice_amount": "125.00"},
    ]

    candidates, review_notes = build_close_candidates(updater, invoices, set(), None)

    assert candidates == [{
        "mva": "012345678",
        "complaint_type": "Glass",
        "vin": "VIN1",
        "invoice_id": "201",
        "cost": "125.00",
        "row_index": "9",
        "received": "2026-02-01",
    }]
    assert review_notes == []


def test_different_mvas_for_same_vin_require_review():
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4, 9]
    updater.get_row_fields.side_effect = [
        {"MVA": "12345678", "Inventory Date": "8/16/2026"},
        {"MVA": "87654321", "Inventory Date": "8/17/2026"},
    ]
    invoice = {
        "subject": "Invoice #201",
        "vin": "VIN1",
        "received": "2026-02-01",
        "invoice_amount": "125.00",
    }

    candidates, review_notes = build_close_candidates(
        updater,
        [invoice],
        set(),
        None,
    )

    assert candidates == []
    assert review_notes == [
        "Invoice #201: VIN VIN1 matched different MVAs: 012345678, 087654321"
    ]


def test_target_mva_filters_other_candidates_and_review_notes():
    updater = MagicMock()
    updater.find_rows_by_vin.side_effect = [[4], []]
    updater.get_row_fields.return_value = {
        "MVA": "12345678",
        "Inventory Date": "8/17/2026",
    }
    invoices = [
        {"subject": "Invoice #201", "vin": "VIN1", "invoice_amount": "125.00"},
        {"subject": "Invoice #202", "vin": "VIN2", "invoice_amount": "306.00"},
    ]

    candidates, review_notes = build_close_candidates(
        updater,
        invoices,
        set(),
        None,
        target_mvas={"099999999"},
    )

    assert candidates == []
    assert review_notes == []


def test_appends_without_replacing_review_queue(tmp_path):
    close_path = tmp_path / "close_workitem.csv"
    close_path.write_text("# reviewed queue\nmva,Type\n011111111,Glass\n", encoding="utf-8")

    _append_candidates(close_path, [{"mva": "022222222", "complaint_type": "Glass"}])

    assert close_path.read_text(encoding="utf-8").splitlines() == [
        "# reviewed queue",
        "mva,Type",
        "011111111,Glass",
        "022222222,Glass",
    ]


def test_retires_processed_rows_and_history_excludes_them(tmp_path):
    close_path = tmp_path / "close_workitem.csv"
    history_path = tmp_path / "close_workitem_history.csv"
    close_path.write_text(
        "# reviewed queue\nmva,Type\n011111111,Glass\n022222222,Glass\n",
        encoding="utf-8",
    )

    retired = _retire_processed_candidates(
        close_path,
        history_path,
        {("011111111", "Glass")},
    )

    assert retired == 1
    assert _load_existing_candidates(close_path) == {("022222222", "Glass")}
    assert _load_existing_candidates(history_path) == {("011111111", "Glass")}