from pathlib import Path
from decimal import Decimal
from unittest.mock import MagicMock, call
import json

import pytest

from WorkItems.build_close_queue import (
    _append_candidates,
    _load_existing_candidates,
    _load_history_reasons,
    _load_uncategorized_invoices,
    _retire_processed_candidates,
    build_close_candidates,
)


def test_selected_closeout_does_not_add_or_select_unrelated_queued_rows(monkeypatch, tmp_path):
    from WorkItems import build_close_queue as module

    queue = tmp_path / "close.csv"
    queue.write_text("mva,Type\n011111111,Glass\n059235573,Glass\n", encoding="utf-8")
    monkeypatch.setattr(module, "DEFAULT_CLOSE_QUEUE", queue)
    monkeypatch.setattr(module, "DEFAULT_CLOSE_HISTORY", tmp_path / "history.csv")
    monkeypatch.setattr(module, "PROCESSED_STATE_PATH", tmp_path / "processed.json")
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.return_value = {"MVA": "59235573", "Inventory Date": "2026-10-22"}

    targets, reviews = module.prepare_selected_closeout(updater, [{
        "subject": "Invoice #5485994", "vin": "5XYP64GC1SG678709",
        "invoice_amount": "340.00",
    }])

    assert reviews == []
    assert [target["mva"] for target in targets] == ["059235573"]
    assert queue.read_text(encoding="utf-8").splitlines() == [
        "mva,Type", "011111111,Glass", "059235573,Glass",
    ]
    updater.update_vendor_fields.assert_called_once_with(
        4, {"Repair Status": module.STATUS_COMPLETED, "Cost": "340.00"},
    )


def test_selected_closeout_sheet_failure_reviews_only_that_invoice(monkeypatch, tmp_path):
    from WorkItems import build_close_queue as module

    queue = tmp_path / "close.csv"
    monkeypatch.setattr(module, "DEFAULT_CLOSE_QUEUE", queue)
    monkeypatch.setattr(module, "DEFAULT_CLOSE_HISTORY", tmp_path / "history.csv")
    monkeypatch.setattr(module, "PROCESSED_STATE_PATH", tmp_path / "processed.json")
    updater = MagicMock()
    updater.find_rows_by_vin.side_effect = [[4], []]
    updater.get_row_fields.return_value = {"MVA": "59235573", "Inventory Date": "2026-10-22"}
    failing = {
        "subject": "Invoice #2", "vin": "VIN2", "invoice_amount": "340.00",
        "selection_index": 1,
    }

    targets, reviews = module.prepare_selected_closeout(updater, [
        {"subject": "Invoice #1", "vin": "VIN1", "invoice_amount": "340.00", "selection_index": 0},
        failing,
    ])

    assert [(target["mva"], target["selection_index"]) for target in targets] == [("059235573", 0)]
    assert reviews == [(failing, "VIN VIN2 not found")]
    updater.update_vendor_fields.assert_called_once_with(
        4, {"Repair Status": module.STATUS_COMPLETED, "Cost": "340.00"},
    )
    assert queue.read_text(encoding="utf-8").splitlines() == ["mva,Type", "059235573,Glass"]


def test_corrupt_close_checkpoint_is_not_silently_ignored(tmp_path):
    from WorkItems.build_close_queue import _load_processed_candidates

    path = tmp_path / "processed.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _load_processed_candidates(path)


def test_selected_history_exclusion_leaves_unrelated_queue_and_sheet_untouched(monkeypatch, tmp_path):
    from WorkItems import build_close_queue as module

    queue = tmp_path / "close.csv"
    content = "mva,Type\n011111111,Glass\n"
    queue.write_text(content, encoding="utf-8")
    history = tmp_path / "history.csv"
    history.write_text(
        "mva,Type,processed_date,result\n059235573,Glass,2026-10-22,closed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DEFAULT_CLOSE_QUEUE", queue)
    monkeypatch.setattr(module, "DEFAULT_CLOSE_HISTORY", history)
    monkeypatch.setattr(module, "PROCESSED_STATE_PATH", tmp_path / "state.json")
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.return_value = {"MVA": "59235573", "Inventory Date": "2026-10-22"}

    assert module.prepare_selected_closeout(updater, [{
        "subject": "Invoice #5485994", "vin": "VIN1", "invoice_amount": "340.00",
    }]) == ([], [])
    updater.ensure_columns.assert_not_called()
    updater.update_vendor_fields.assert_not_called()
    assert queue.read_text(encoding="utf-8") == content


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
    from invoice_workflow.invoice_parser import InvoicePdfData

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
    monkeypatch.setattr(
        "WorkItems.build_close_queue.parse_invoice_pdf",
        lambda path: InvoicePdfData("FPO1131464", "1HGBH41JXMN109186", Decimal("306.00")),
    )

    invoices, review_notes = _load_uncategorized_invoices()

    assert [row["subject"] for row in invoices] == [
        "Invoice #201",
        "Invoice #202",
    ]
    assert review_notes == []
    categorized.Attachments[0].SaveAsFile.assert_not_called()
    receipt.Attachments[0].SaveAsFile.assert_not_called()


def test_recalibration_never_creates_a_glass_close_candidate():
    updater = MagicMock()
    candidates, notes, skipped = build_close_candidates(
        updater,
        [{
            "subject": "Invoice #5426981",
            "vin": "5XYP6DGC9SG688906",
            "invoice_amount": "150.00",
            "is_recalibration": True,
        }],
        {},
        None,
    )
    assert candidates == []
    assert notes == []
    assert len(skipped) == 1
    assert "Invoice #5426981" in skipped[0]
    assert "recalibration; separate PO/invoice validation" in skipped[0]
    updater.find_rows_by_vin.assert_not_called()


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

    candidates, review_notes, skipped = build_close_candidates(updater, invoices, {}, None)

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
    assert skipped == []


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

    candidates, review_notes, skipped = build_close_candidates(
        updater,
        [invoice],
        {},
        None,
    )

    assert candidates == []
    assert review_notes == [
        "Invoice #201: VIN VIN1 matched different MVAs: 012345678, 087654321"
    ]
    assert skipped == []


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

    candidates, review_notes, skipped = build_close_candidates(
        updater,
        invoices,
        {},
        None,
        target_mvas={"099999999"},
    )

    assert candidates == []
    assert review_notes == []
    assert len(skipped) == 2
    assert "outside requested --mva scope" in skipped[0]
    assert "cannot establish requested MVA scope: VIN VIN2 not found" in skipped[1]


def test_every_parsed_invoice_has_a_candidate_or_explanation():
    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.side_effect = [
        {"MVA": mva, "Inventory Date": "10/21/2026"}
        for mva in ("12345678", "12345678", "87654321", "11111111", "22222222")
    ]
    invoices = [
        {"subject": f"Invoice #{number}", "vin": f"VIN{number}"}
        for number in range(201, 206)
    ]
    invoices.extend([
        {"subject": "Invoice #206", "vin": "VIN206", "is_recalibration": True},
    ])
    candidates, review, skipped = build_close_candidates(
        updater,
        invoices,
        {("087654321", "Glass"): "previously handled; recorded in close history"},
        1,
    )
    assert len(candidates) == 1
    assert review == []
    assert len(candidates) + len(review) + len(skipped) == len(invoices)
    assert "Invoice #202 | VIN VIN202 | MVA 012345678" in skipped[0]
    assert "already selected in this batch by Invoice 201" in skipped[0]
    assert "Invoice #203" in skipped[1]
    assert "recorded in close history" in skipped[1]
    assert "Invoice #204" in skipped[2]
    assert "deferred by --max-rows 1" in skipped[2]
    assert "Invoice #205" in skipped[3]
    assert "deferred by --max-rows 1" in skipped[3]
    assert "Invoice #206" in skipped[4]
    assert "recalibration" in skipped[4]


@pytest.mark.parametrize("apply", [False, True])
def test_console_reports_each_exclusion_with_its_exact_source(
    monkeypatch, tmp_path, capsys, apply
):
    import WorkItems.build_close_queue as module

    updater = MagicMock()
    updater.find_rows_by_vin.return_value = [4]
    updater.get_row_fields.side_effect = [
        {"MVA": mva, "Inventory Date": "10/21/2026"}
        for mva in ("12345678", "12345678", "87654321", "11111111", "22222222")
    ]
    invoices = [
        {"subject": f"Invoice #{number}", "vin": f"VIN{number}"}
        for number in range(201, 206)
    ]
    invoices.append(
        {"subject": "Invoice #206", "vin": "VIN206", "is_recalibration": True}
    )
    close_path = tmp_path / "close.csv"
    history_path = tmp_path / "history.csv"
    history_path.write_text(
        "mva,Type,processed_date,result\n011111111,Glass,2026-10-20,not_found\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "VendorSheetUpdater", lambda *_args: updater)
    monkeypatch.setattr(module, "_load_config", lambda: {"spreadsheet_id": "sheet-id"})
    monkeypatch.setattr(module, "_load_uncategorized_invoices", lambda: (invoices, []))
    monkeypatch.setattr(
        module, "_load_existing_candidates",
        lambda path: (
            {("087654321", "Glass")} if path == close_path
            else {("011111111", "Glass")}
        ),
    )
    monkeypatch.setattr(
        module, "_load_processed_candidates", lambda _path: {("022222222", "Glass")}
    )
    append = MagicMock()
    monkeypatch.setattr(module, "_append_candidates", append)
    monkeypatch.setattr(module, "_retire_processed_candidates", lambda *_args: 0)
    monkeypatch.setattr(
        "sys.argv",
        ["build_close_queue.py", "--close-queue", str(close_path),
         "--history", str(history_path)] + (["--apply"] if apply else []),
    )

    assert module.main() == 0
    output = capsys.readouterr().out
    assert f"{'APPLY' if apply else 'DRY RUN'}: 6 invoice(s), 1 new candidate(s)" in output
    assert "No new candidate: 5 invoice(s)" in output
    assert "Invoice #202 | VIN VIN202 | MVA 012345678: same MVA/Glass" in output
    assert "Invoice #203 | VIN VIN203 | MVA 087654321: already in the close queue" in output
    assert "Invoice #204 | VIN VIN204 | MVA 011111111: close history 2026-10-20" in output
    assert "no open work item was found; no work item was closed" in output
    assert "Invoice #205 | VIN VIN205 | MVA 022222222: already checkpointed today" in output
    assert "Invoice #206 | VIN VIN206: recalibration" in output
    assert "Needs review:" not in output
    if apply:
        assert len(append.call_args.args[1]) == 1
        updater.update_vendor_fields.assert_called_once()
    else:
        append.assert_not_called()
        updater.update_vendor_fields.assert_not_called()


def test_console_does_not_truncate_source_review_reasons(monkeypatch, capsys):
    import WorkItems.build_close_queue as module

    monkeypatch.setattr(module, "VendorSheetUpdater", MagicMock())
    monkeypatch.setattr(module, "_load_config", lambda: {"spreadsheet_id": "sheet-id"})
    monkeypatch.setattr(
        module, "_load_uncategorized_invoices",
        lambda: ([], [f"Invoice #{number}: no PDF attachment" for number in range(21)]),
    )
    monkeypatch.setattr(module, "_load_existing_candidates", lambda _path: set())
    monkeypatch.setattr(module, "_load_processed_candidates", lambda _path: set())
    monkeypatch.setattr(module, "_load_history_reasons", lambda _path: {})
    monkeypatch.setattr("sys.argv", ["build_close_queue.py"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "Needs review: 21 invoice(s) failed source or Sheet checks" in output
    assert "Invoice #20: no PDF attachment" in output


def test_history_explains_each_record_without_claiming_invoice_identity(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "mva,Type,processed_date,result\n"
        "059753525,Glass,2026-10-20,not_found\n"
        "011111111,Glass,2026-10-19,closed\n"
        "011111111,Glass,2026-10-20,checkpointed\n",
        encoding="utf-8",
    )
    reasons = _load_history_reasons(path)
    assert "2026-10-20: no open work item was found; no work item was closed" in reasons[("059753525", "Glass")]
    assert "[result=not_found]" in reasons[("059753525", "Glass")]
    assert "History does not record VIN, PO, or invoice number" in reasons[("059753525", "Glass")]
    assert "2026-10-19: work item was marked complete" in reasons[("011111111", "Glass")]
    assert "closure outcome is not recorded" in reasons[("011111111", "Glass")]


def test_history_invalid_schema_is_reported(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text("mva,Type\n059753525,Glass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        _load_history_reasons(path)


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