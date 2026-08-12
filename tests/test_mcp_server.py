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
    _insert(conn, sid, "shiori_test_mcp_target", QUERY_EMB, now)
    monkeypatch.setattr(query, "embed_query", lambda q: QUERY_EMB)
    monkeypatch.setattr(query, "get_db", lambda: _NoCloseConn(conn))

    result = _call(server, "search", {"query": "shiori_test_mcp", "limit": 10})
    data = _parse(result)
    assert data["count"] >= 1
    for r in data["results"]:
        assert {"content", "score", "timestamp", "session_id", "source_type"} <= set(r)
    assert any(r["content"] == "shiori_test_mcp_target" for r in data["results"])


def test_limit_is_clamped_to_max(server, monkeypatch):
    captured = {}

    def fake_search(q, limit=5):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(query, "search", fake_search)
    _call(server, "search", {"query": "anything", "limit": 100})
    # The bounded page asks for one look-ahead row so has_more is truthful.
    assert captured["limit"] == 21


def test_default_limit_is_5(server, monkeypatch):
    captured = {}

    def fake_search(q, limit=5):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(query, "search", fake_search)
    _call(server, "search", {"query": "anything"})
    assert captured["limit"] == 6


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


def _mcp_tuple_row(i=1):
    return (
        f"content-{i}",
        0.9,
        datetime(2026, 1, i, tzinfo=UTC),
        f"session-{i}",
        "main_user",
        "voyage-4-large",
        1024,
    )


def _mcp_dict_row(i=1):
    return {
        "content": f"content-{i}",
        "score": 0.9,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "session_id": f"session-{i}",
        "source_type": "main_user",
        "embedding_model": "voyage-4-large",
        "embedding_dimension": 1024,
        "provenance": {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": f"session-{i}",
            "source_type": "main_user",
            "embedding_model": "voyage-4-large",
            "embedding_dimension": 1024,
        },
        "explain": {
            "score_kind": "rrf",
            "adjustments": [],
            "channels": {
                "dense": {"matched": True, "candidate_rank": 1},
                "lexical": {"matched": True, "candidate_rank": 1},
                "exact": {"matched": True, "candidate_rank": 1},
            },
            "matched_channel_count": 3,
            "multi_channel": True,
        },
    }


def test_run_search_explain_preserves_default_and_contract(monkeypatch):
    """Phase 4F1 slice3 genuine red (task #39).

    ``mcp_server.run_search(..., explain=True)`` must produce the same JSON
    payload shape as the default (same pagination/count fields), with each
    result keeping its existing top-level fields and ``provenance`` unchanged
    and gaining ONLY the frozen ``explain`` sub-dict (no provenance inside
    explain).  ``explain=False`` and the omitted form must produce identical
    payload dicts (key/value/iteration order equal).

    The seam is public ``mcp_server.run_search``; ``query.search_page`` is
    stubbed to record calls and return tuple rows by default / slice1-shaped
    dict rows (full top-level fields + provenance + frozen explain) when
    ``explain=True``.  No product-code/schema/_search_tool edits.

    On the current head this node fails ONLY because public ``run_search``
    does not accept an ``explain`` keyword argument.
    """
    calls = []

    def fake_search_page(text, *, limit=5, offset=0, filters=None, explain=False):
        calls.append(explain)
        rows = (
            [_mcp_tuple_row(i) for i in range(1, limit + 1)]
            if not explain
            else [_mcp_dict_row(i) for i in range(1, limit + 1)]
        )
        return query.SearchPage(
            results=rows,
            limit=limit,
            offset=offset,
            has_more=True,
            next_offset=offset + limit,
        )

    monkeypatch.setattr(query, "search_page", fake_search_page)

    omitted = mcp_server.run_search("probe")
    false_explicit = mcp_server.run_search("probe", explain=False)
    true_explicit = mcp_server.run_search("probe", explain=True)

    # omitted and explain=False payload dicts are fully identical.
    assert false_explicit == omitted
    assert list(false_explicit) == list(omitted)
    assert calls == [False, False, True]

    # true payload keeps pagination/count fields; results grow only explain.
    for key in ("limit", "offset", "has_more", "next_offset", "count"):
        assert true_explicit[key] == omitted[key]
    assert "error" not in true_explicit

    assert len(true_explicit["results"]) == len(omitted["results"])
    for row, ref in zip(true_explicit["results"], omitted["results"]):
        assert row["content"] == ref["content"]
        assert row["score"] == ref["score"]
        # Existing top-level fields and provenance unchanged.
        for key in ("timestamp", "session_id", "source_type",
                    "embedding_model", "embedding_dimension", "provenance"):
            assert row[key] == ref[key]
        # Frozen explain added, with no provenance inside it.
        assert row["explain"] == _mcp_dict_row()["explain"]
        assert "provenance" not in row["explain"]
