from pathlib import Path
from unittest.mock import MagicMock, call

from WorkItems.build_close_queue import (
    _append_candidates,
    _load_existing_candidates,
    _load_uncategorized_receipts,
    _retire_processed_candidates,
    build_close_candidates,
)


def test_apply_ensures_vendor_columns_before_processing(monkeypatch, tmp_path):
    import WorkItems.build_close_queue as build_close_queue

    updater = MagicMock()
    monkeypatch.setattr(build_close_queue, "VendorSheetUpdater", MagicMock(return_value=updater))
    monkeypatch.setattr(build_close_queue, "_load_config", lambda: {"spreadsheet_id": "sheet-id"})
    monkeypatch.setattr(build_close_queue, "_load_uncategorized_receipts", lambda: ([], []))
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


def test_loads_only_uncategorized_outlook_receipts_oldest_first(monkeypatch):
    import outlook.agn_invoices as agn_invoices

    def make_mail(subject, received, categorized=False):
        attachment = MagicMock()
        attachment.FileName = "receipt.pdf"
        mail = MagicMock()
        mail.Class = 43
        mail.Subject = subject
        mail.ReceivedTime = received
        mail.Categories = "Green Category" if categorized else ""
        mail.Attachments = [attachment]
        return mail

    newer = make_mail("Receipt for Job #102", "2026-02-02T08:00:00")
    older = make_mail("Receipt for Job #101", "2026-02-01T08:00:00")
    categorized = make_mail("Receipt for Job #100", "2026-01-01T08:00:00", categorized=True)
    invoice = make_mail("Invoice #200", "2026-01-02T08:00:00")
    folder = MagicMock()
    folder.Items = [newer, categorized, invoice, older]

    monkeypatch.setattr(agn_invoices, "get_invoice_folder", lambda: folder)
    monkeypatch.setattr(agn_invoices, "extract_receipt_vin_and_amount", lambda path: ("1HGBH41JXMN109186", "306.00"))

    receipts, review_notes = _load_uncategorized_receipts()

    assert [row["subject"] for row in receipts] == [
        "Receipt for Job #101",
        "Receipt for Job #102",
    ]
    assert review_notes == []
    categorized.Attachments[0].SaveAsFile.assert_not_called()
    invoice.Attachments[0].SaveAsFile.assert_not_called()


def test_builds_only_strict_unique_vin_candidates():
    updater = MagicMock()
    updater.find_unique_row_by_vin.side_effect = [
        type("Match", (), {"is_ok": True, "row_index": 4, "status": "ok", "note": ""})(),
        type(
            "Match",
            (),
            {"is_ok": False, "row_index": None, "status": "ambiguous", "note": "VIN matched 2 rows"},
        )(),
    ]
    updater.get_row_fields.return_value = {"MVA": "12345678"}
    receipts = [
        {"subject": "Receipt for Job #101", "vin": "VIN1", "received": "2026-02-01", "invoice_amount": "125.00"},
        {"subject": "Receipt for Job #102", "vin": "VIN2", "received": "2026-02-02", "invoice_amount": "306.00"},
    ]

    candidates, review_notes = build_close_candidates(updater, receipts, set(), None)

    assert candidates == [{
        "mva": "012345678",
        "complaint_type": "Glass",
        "vin": "VIN1",
        "job_id": "101",
        "cost": "125.00",
        "row_index": "4",
        "received": "2026-02-01",
        "_mail": None,
    }]
    assert review_notes == ["Receipt for Job #102: VIN matched 2 rows"]


def test_recovers_uncategorized_receipt_already_in_active_queue():
    updater = MagicMock()
    updater.find_unique_row_by_vin.return_value = type(
        "Match", (), {"is_ok": True, "row_index": 4, "status": "ok", "note": ""}
    )()
    updater.get_row_fields.return_value = {"MVA": "12345678"}
    receipt = {
        "subject": "Receipt for Job #101",
        "vin": "VIN1",
        "received": "2026-02-01",
        "invoice_amount": "125.00",
    }
    queued = {("012345678", "Glass")}

    candidates, review_notes = build_close_candidates(
        updater,
        [receipt],
        queued,
        None,
        recoverable_existing=queued,
    )

    assert review_notes == []
    assert len(candidates) == 1
    assert candidates[0]["_already_queued"] is True


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