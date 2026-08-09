import os
from datetime import datetime, timezone

import pytest

import ingest

MTIME = datetime(2026, 8, 3, 5, 0, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, fetch_value=True):
        self._fetch_value = fetch_value

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return (self._fetch_value,)

    def close(self):
        pass


class FakeConn:
    def __init__(self, locked=True):
        self._locked = locked
        self.closed = False

    def cursor(self):
        return FakeCursor(fetch_value=self._locked)

    def close(self):
        self.closed = True

    def rollback(self):
        pass

    def commit(self):
        pass


@pytest.fixture
def clean_argv(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ingest.py"])


@pytest.fixture
def no_db(monkeypatch):
    """Replace main()'s DB + file + checkpoint boundaries with a controllable fake."""
    conn = FakeConn(locked=True)
    calls = {"find": [], "processed": {}, "mark": [], "parse": {}}

    monkeypatch.setattr(ingest, "get_db", lambda: conn)
    monkeypatch.setattr(ingest, "find_session_files", lambda: list(calls["find"]))
    monkeypatch.setattr(ingest, "get_processed_files", lambda c: dict(calls["processed"]))
    monkeypatch.setattr(ingest, "mark_file_processed",
                        lambda c, p, m, s, st, cc, partial=False: calls["mark"].append((p, st, cc, partial)))
    monkeypatch.setattr(ingest, "parse_session_file", lambda p: list(calls["parse"].get(p, [])))
    return conn, calls


def _make_file(tmp_path, name, size_bytes=100, mtime=None):
    p = tmp_path / name
    p.write_text("x" * size_bytes)
    if mtime is not None:
        os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    st = os.stat(p)
    return str(p), datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size


def test_main_skips_file_when_mtime_and_size_unchanged(no_db, tmp_path, clean_argv):
    conn, calls = no_db
    path, mtime, size = _make_file(tmp_path, "abc.jsonl", mtime=MTIME)
    calls["find"] = [path]
    calls["processed"] = {path: {"mtime": mtime, "size": size}}

    ingest.main()

    assert calls["mark"] == []
    assert conn.closed


def test_main_reprocesses_when_size_changed(no_db, tmp_path, clean_argv):
    conn, calls = no_db
    path, mtime, _ = _make_file(tmp_path, "abc.jsonl", size_bytes=200, mtime=MTIME)
    calls["find"] = [path]
    # recorded checkpoint has a different size → mismatch → reprocess
    calls["processed"] = {path: {"mtime": mtime, "size": 999}}
    calls["parse"][path] = []

    ingest.main()

    assert calls["mark"], "file should be reprocessed and checkpointed"
    marked_path, st, cc, partial = calls["mark"][0]
    assert marked_path == path
    assert st == "empty"


def test_main_force_ignores_checkpoint(no_db, tmp_path, monkeypatch, clean_argv):
    conn, calls = no_db
    path, mtime, size = _make_file(tmp_path, "abc.jsonl", mtime=MTIME)
    calls["find"] = [path]
    calls["processed"] = {path: {"mtime": mtime, "size": size}}
    calls["parse"][path] = []
    monkeypatch.setattr("sys.argv", ["ingest.py", "--force"])

    ingest.main()

    assert calls["mark"], "--force must bypass unchanged check and reprocess"


def test_main_marks_empty_file_checkpoint(no_db, tmp_path, clean_argv):
    conn, calls = no_db
    path, _, _ = _make_file(tmp_path, "empty.jsonl")
    calls["find"] = [path]
    calls["parse"][path] = []  # no messages → empty

    ingest.main()

    assert len(calls["mark"]) == 1
    marked_path, st, cc, partial = calls["mark"][0]
    assert st == "empty"
    assert cc == 0


def test_main_marks_cron_file_checkpoint(no_db, tmp_path, monkeypatch, clean_argv):
    conn, calls = no_db
    path, _, _ = _make_file(tmp_path, "cron.jsonl")
    calls["find"] = [path]
    calls["parse"][path] = [{"type": "message"}]
    monkeypatch.setattr(ingest, "classify_session", lambda fp, lines: "cron")

    ingest.main()

    assert len(calls["mark"]) == 1
    marked_path, st, cc, partial = calls["mark"][0]
    assert st == "cron"
    assert cc == 0
