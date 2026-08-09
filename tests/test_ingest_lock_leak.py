import pytest

import ingest


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


def test_get_processed_files_error_still_releases_lock(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(ingest, "get_db", lambda: conn)
    monkeypatch.setattr(ingest, "find_session_files", lambda: [])

    def boom(conn):
        raise RuntimeError("get_processed_files failed")

    monkeypatch.setattr(ingest, "get_processed_files", boom)
    monkeypatch.setattr("sys.argv", ["ingest.py"])

    with pytest.raises(RuntimeError):
        ingest.main()

    assert conn.closed, "conn must be closed when get_processed_files raises"
    unlock_calls = [
        (sql, p) for sql, p in conn.executed
        if "pg_advisory_unlock" in sql
    ]
    assert unlock_calls, "advisory lock must be released when get_processed_files raises"


# ── Cycle 5: re-lock after reconnect (NB-C5-01) ─────────────────────────────
# A per-file exception that makes conn.rollback() fail triggers a reconnect
# (conn.close() + get_db()). The new connection does NOT inherit the advisory
# lock, so it must be re-acquired — or processing would continue unlocked.

class ReconnectConn(FakeConn):
    def __init__(self, fail_rollback=False, fetchone=(True,)):
        super().__init__()
        self.fail_rollback = fail_rollback
        self._fetchone = fetchone

    def rollback(self):
        if self.fail_rollback:
            raise RuntimeError("rollback failed")
        return super().rollback()

    def cursor(self):
        return ReconnectCursor(self.executed, self._fetchone)


class ReconnectCursor(FakeCursor):
    def __init__(self, executed, fetchone):
        super().__init__(executed)
        self._fetchone = fetchone


def _one_failing_file(tmp_path, parse_raises=True):
    src = tmp_path / "boom.jsonl"
    src.write_text('{"type":"message","role":"user","content":"hi"}\n')
    return str(src)


def _make_boom_parse(filepath):
    def boom(path):
        raise RuntimeError("parse boom")
    return boom


def test_reconnect_reacquires_lock(monkeypatch, tmp_path):
    src = _one_failing_file(tmp_path)
    conn1 = ReconnectConn(fail_rollback=True)
    conn2 = ReconnectConn()
    seq = iter([conn1, conn2])
    monkeypatch.setattr(ingest, "get_db", lambda: next(seq))
    monkeypatch.setattr(ingest, "find_session_files", lambda: [src])
    monkeypatch.setattr(ingest, "get_processed_files", lambda c: {})
    monkeypatch.setattr(ingest, "parse_session_file", _make_boom_parse(src))
    monkeypatch.setattr("sys.argv", ["ingest.py"])

    ingest.main()

    assert conn2.closed, "reconnected conn must be closed"
    relock = [(sql, p) for sql, p in conn2.executed if "pg_try_advisory_lock" in sql]
    assert relock, "reconnected conn must re-acquire the advisory lock"
    unlock = [(sql, p) for sql, p in conn2.executed if "pg_advisory_unlock" in sql]
    assert unlock, "reconnected conn must release the lock on exit"


def test_reconnect_relock_failure_aborts(monkeypatch, tmp_path):
    src = _one_failing_file(tmp_path)
    conn1 = ReconnectConn(fail_rollback=True)
    # Relock attempt returns False → another instance holds the lock.
    conn2 = ReconnectConn(fetchone=(False,))
    seq = iter([conn1, conn2])
    monkeypatch.setattr(ingest, "get_db", lambda: next(seq))
    monkeypatch.setattr(ingest, "find_session_files", lambda: [src])
    monkeypatch.setattr(ingest, "get_processed_files", lambda c: {})
    monkeypatch.setattr(ingest, "parse_session_file", _make_boom_parse(src))
    monkeypatch.setattr("sys.argv", ["ingest.py"])

    ingest.main()

    assert conn2.closed, "reconnected conn must be closed even when relock fails"
    relock = [(sql, p) for sql, p in conn2.executed if "pg_try_advisory_lock" in sql]
    assert relock, "must attempt to re-acquire the lock"
    unlock = [(sql, p) for sql, p in conn2.executed if "pg_advisory_unlock" in sql]
    assert not unlock, "must not release a lock it failed to re-acquire"
