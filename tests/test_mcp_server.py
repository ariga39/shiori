import asyncio
import json
from datetime import UTC, datetime

import pytest

import mcp_server
import query

QUERY_EMB = [1.0] + [0.0] * 1023


def _insert(conn, sid, content, emb, ts):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,%s,%s,0,0,to_tsvector('simple',%s),%s)""",
        (sid, "main_user", content, str(emb), "voyage-4-large",
         ts, ts, content, ts),
    )
    conn.commit()
    cur.close()


class _NoCloseConn:
    """Wraps the fixture connection so query.search can borrow it without
    closing it (the db fixture owns and cleans it up in teardown)."""

    def __init__(self, real):
        self._real = real

    def cursor(self):
        return self._real.cursor()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        pass


def _parse(result):
    return json.loads(result.content[0].text)


def _list_tools(server):
    return asyncio.run(server.list_tools())


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


@pytest.fixture
def server():
    return mcp_server.build_server()


def test_tool_list_contains_search(server):
    tools = _list_tools(server)
    assert "search" in [t.name for t in tools]


def test_search_returns_structured_dicts(db, server, monkeypatch):
    conn, prefix = db
    sid = prefix + "-mcp"
    now = datetime.now(UTC)
    _insert(conn, sid, "shiyi_test_mcp_target", QUERY_EMB, now)
    monkeypatch.setattr(query, "embed_query", lambda q: QUERY_EMB)
    monkeypatch.setattr(query, "get_db", lambda: _NoCloseConn(conn))

    result = _call(server, "search", {"query": "shiyi_test_mcp", "limit": 10})
    data = _parse(result)
    assert data["count"] >= 1
    for r in data["results"]:
        assert {"content", "score", "timestamp", "session_id", "source_type"} <= set(r)
    assert any(r["content"] == "shiyi_test_mcp_target" for r in data["results"])


def test_limit_is_clamped_to_max(server, monkeypatch):
    captured = {}

    def fake_search(q, limit=5):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(query, "search", fake_search)
    _call(server, "search", {"query": "anything", "limit": 100})
    assert captured["limit"] == 20


def test_default_limit_is_5(server, monkeypatch):
    captured = {}

    def fake_search(q, limit=5):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(query, "search", fake_search)
    _call(server, "search", {"query": "anything"})
    assert captured["limit"] == 5


def test_empty_query_returns_error(server):
    result = _call(server, "search", {"query": "   "})
    data = _parse(result)
    assert "error" in data


def test_search_failure_mapped_to_readable_error(server, monkeypatch):
    def boom(q, limit=5):
        raise RuntimeError("postgresql://user:synthetic-secret@example.test/db")

    monkeypatch.setattr(query, "search", boom)
    result = _call(server, "search", {"query": "anything"})
    data = _parse(result)
    assert data["error"] == {"code": "search_failed", "type": "RuntimeError"}
    assert "synthetic-secret" not in json.dumps(data)
