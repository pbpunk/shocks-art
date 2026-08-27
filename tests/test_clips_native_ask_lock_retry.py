from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

import app.services.clips_native_ask as clips_native_ask


class FakeSession:
    def __init__(self, stream_id: str):
        self.stream = SimpleNamespace(stream_id=stream_id)
        self.rollback_count = 0
        self.commit_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, model, stream_id):
        return self.stream if stream_id == self.stream.stream_id else None

    def rollback(self):
        self.rollback_count += 1

    def commit(self):
        self.commit_count += 1


def test_persist_native_ask_retries_same_response_after_sqlite_lock(monkeypatch):
    sessions = [FakeSession("stream_fixture"), FakeSession("stream_fixture")]
    monkeypatch.setattr(clips_native_ask, "SessionLocal", lambda: sessions.pop(0))

    calls = []

    def fake_save(db, stream, response_text):
        calls.append(response_text)
        if len(calls) == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        run = SimpleNamespace(analysis_run_id="run_ok")
        candidate = SimpleNamespace(candidate_window_id="candidate_ok")
        return run, [candidate], 0

    monkeypatch.setattr(clips_native_ask, "save_clips_native_ask_response", fake_save)
    sleeps = []

    result = clips_native_ask._persist_native_ask_with_lock_retry(
        "stream_fixture",
        "the exact browser response",
        attempts=2,
        sleep_fn=sleeps.append,
    )

    assert result == ("run_ok", ["candidate_ok"], 0)
    assert calls == ["the exact browser response", "the exact browser response"]
    assert sleeps == [0.5]


def test_database_lock_detection_walks_exception_chain():
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = OperationalError("INSERT", {}, Exception("database is locked"))
    assert clips_native_ask._is_sqlite_locked(wrapped) is True
    assert clips_native_ask._is_sqlite_locked(RuntimeError("validation failed")) is False
