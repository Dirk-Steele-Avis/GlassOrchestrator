from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ProcessLocalMarket_PMs as local_market


def test_uses_all_purpose_auto_folder_directly_under_inbox():
    assert local_market.MAIL_FOLDER_PATH == ("Inbox", "AllPurposeAuto")


class _PdfPage:
    def __init__(self, words):
        self._words = words

    def extract_words(self):
        return self._words


class _Pdf:
    def __init__(self, words):
        self.pages = [_PdfPage(words)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_extracts_only_valid_values_from_mva_table_column(monkeypatch):
    words = [
        {"text": "Model", "x0": 130, "x1": 150, "top": 160, "bottom": 168},
        {"text": "MVA", "x0": 195, "x1": 208, "top": 160, "bottom": 168},
        {"text": "Mileage", "x0": 240, "x1": 265, "top": 160, "bottom": 168},
        {"text": "59196001", "x0": 190, "x1": 213, "top": 176, "bottom": 182},
        {"text": "058524185", "x0": 188, "x1": 215, "top": 190, "bottom": 196},
        {"text": "not-an-mva", "x0": 188, "x1": 215, "top": 204, "bottom": 210},
        {"text": "12345678", "x0": 242, "x1": 265, "top": 176, "bottom": 182},
    ]
    monkeypatch.setattr(local_market.pdfplumber, "open", lambda _path: _Pdf(words))

    assert local_market.extract_mvas_from_pdf(Path("orders.pdf")) == [
        "59196001",
        "058524185",
    ]


def test_pdf_without_mva_column_fails_strictly(monkeypatch):
    words = [
        {"text": "Model", "x0": 130, "x1": 150, "top": 160, "bottom": 168},
        {"text": "Mileage", "x0": 240, "x1": 265, "top": 160, "bottom": 168},
    ]
    monkeypatch.setattr(local_market.pdfplumber, "open", lambda _path: _Pdf(words))

    with pytest.raises(RuntimeError, match=r"No Model \| MVA \| Mileage"):
        local_market.extract_mvas_from_pdf(Path("orders.pdf"))


def test_deduplicates_in_first_seen_order():
    assert local_market.deduplicate_mvas(
        ["59196001", "058524185", "59196001", "12345678"]
    ) == ["59196001", "058524185", "12345678"]


def _attachment(name):
    attachment = MagicMock()
    attachment.FileName = name
    attachment.SaveAsFile.side_effect = lambda path: Path(path).touch()
    return attachment


def _message(*, unread=True, sender=local_market.SENDER_ADDRESS, attachments=()):
    return SimpleNamespace(
        Class=43,
        UnRead=unread,
        SenderEmailAddress=sender,
        ReceivedTime="2026-08-25 08:00:00",
        Attachments=list(attachments),
    )


def test_collects_all_pdfs_from_only_matching_unread_sender(monkeypatch):
    first_pdf = _attachment("first.pdf")
    second_pdf = _attachment("second.PDF")
    matching = _message(attachments=[first_pdf, second_pdf])
    folder = SimpleNamespace(
        Items=[
            _message(unread=False, attachments=[_attachment("read.pdf")]),
            _message(sender="someone@example.com", attachments=[_attachment("other.pdf")]),
            matching,
        ]
    )
    parsed = iter([["59196001", "058524185"], ["59196001", "12345678"]])
    monkeypatch.setattr(local_market, "extract_mvas_from_pdf", lambda _path: next(parsed))

    result = local_market.collect_unread_mvas(folder)

    assert result.mvas == ["59196001", "058524185", "12345678"]
    assert result.source_messages == [matching]
    first_pdf.SaveAsFile.assert_called_once()
    second_pdf.SaveAsFile.assert_called_once()


def test_development_run_leaves_source_messages_unread(monkeypatch):
    message = MagicMock()
    message.UnRead = True
    collected = local_market.CollectedVend(["59196001"], [message])
    monkeypatch.setattr(local_market, "setup_logging", lambda: None)
    monkeypatch.setattr(local_market, "parse_args", lambda: SimpleNamespace(submit=False))
    monkeypatch.setattr(local_market, "collect_unread_mvas", lambda: collected)
    run_compass = MagicMock()
    mark_read = MagicMock()
    monkeypatch.setattr(local_market, "run_compass", run_compass)
    monkeypatch.setattr(local_market, "mark_messages_read", mark_read)

    assert local_market.run() == 0
    run_compass.assert_called_once_with(["59196001"], submit=False)
    mark_read.assert_not_called()
    assert message.UnRead is True


def test_submit_marks_messages_read_only_after_compass_returns(monkeypatch):
    events = []
    message = MagicMock()
    collected = local_market.CollectedVend(["59196001"], [message])
    monkeypatch.setattr(local_market, "setup_logging", lambda: None)
    monkeypatch.setattr(local_market, "parse_args", lambda: SimpleNamespace(submit=True))
    monkeypatch.setattr(local_market, "collect_unread_mvas", lambda: collected)
    monkeypatch.setattr(
        local_market,
        "run_compass",
        lambda mvas, submit: events.append(("compass", mvas, submit)),
    )
    monkeypatch.setattr(
        local_market,
        "mark_messages_read",
        lambda messages: events.append(("read", messages)),
    )

    assert local_market.run() == 0
    assert events == [
        ("compass", ["59196001"], True),
        ("read", [message]),
    ]