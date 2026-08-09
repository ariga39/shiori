import pytest

import ingest_discord


class FakeCursor:
    def __init__(self, executed):
        self.executed = executed
        self._fetchone = (True,)
        self._fetchall = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.closed = False
        self.executed = []

    def cursor(self):
        return FakeCursor(self.executed)

    def close(self):
        self.closed = True

    def rollback(self):
        pass

    def commit(self):
        pass


def _run_empty_archive(monkeypatch, tmp_path):
    empty_dir = tmp_path / "empty-archive"
    empty_dir.mkdir()
    monkeypatch.setattr(ingest_discord, "ARCHIVE_DIR", empty_dir)

    conn = FakeConn()
    monkeypatch.setattr(ingest_discord, "get_db", lambda: conn)
    monkeypatch.setattr("sys.argv", ["ingest_discord.py"])
    ingest_discord.main()
    return conn


def test_empty_archive_releases_lock_and_closes_conn(monkeypatch, tmp_path):
    conn = _run_empty_archive(monkeypatch, tmp_path)
    assert conn.closed, "conn must be closed even when archive is empty"
    unlock_calls = [
        (sql, p) for sql, p in conn.executed
        if "pg_advisory_unlock" in sql
    ]
    assert unlock_calls, "advisory lock must be released on empty-archive early return"


def test_get_processed_files_error_still_releases_lock(monkeypatch, tmp_path):
    empty_dir = tmp_path / "empty-archive-err"
    empty_dir.mkdir()
    monkeypatch.setattr(ingest_discord, "ARCHIVE_DIR", empty_dir)

    conn = FakeConn()
    monkeypatch.setattr(ingest_discord, "get_db", lambda: conn)

    def boom(conn):
        raise RuntimeError("get_processed_files failed")

    monkeypatch.setattr(ingest_discord, "get_processed_files", boom)
    monkeypatch.setattr("sys.argv", ["ingest_discord.py"])

    with pytest.raises(RuntimeError):
        ingest_discord.main()

    assert conn.closed, "conn must be closed when get_processed_files raises"
    unlock_calls = [
        (sql, p) for sql, p in conn.executed
        if "pg_advisory_unlock" in sql
    ]
    assert unlock_calls, "advisory lock must be released when get_processed_files raises"
