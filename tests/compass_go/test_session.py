from types import SimpleNamespace

from src.compass_go import session


def test_is_edge_running_false_when_tasklist_has_no_match(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout="INFO: No tasks are running which match the specified criteria.\n")

    monkeypatch.setattr(session.subprocess, "run", _fake_run)

    assert session._is_edge_running() is False


def test_is_edge_running_true_when_msedge_row_present(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout="msedge.exe                    1234 Console                    1     12,000 K\n")

    monkeypatch.setattr(session.subprocess, "run", _fake_run)

    assert session._is_edge_running() is True


def test_kill_running_edge_retries_until_cleared(monkeypatch):
    states = iter([True, True, False])
    calls = []

    monkeypatch.setattr(session, "_is_edge_running", lambda: next(states))
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    def _fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(session.subprocess, "run", _fake_run)

    assert session.kill_running_edge() is True
    assert len(calls) == 2


def test_kill_running_edge_returns_false_when_still_running(monkeypatch):
    monkeypatch.setattr(session, "EDGE_KILL_MAX_ATTEMPTS", 2)
    states = iter([True, True, True])
    calls = []

    monkeypatch.setattr(session, "_is_edge_running", lambda: next(states))
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)

    def _fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(session.subprocess, "run", _fake_run)

    assert session.kill_running_edge() is False
    assert len(calls) == 2