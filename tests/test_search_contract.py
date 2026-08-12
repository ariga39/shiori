from datetime import UTC, datetime

import pytest

import mcp_server
import query


def _row(i: int = 1):
    return (
        f"content-{i}",
        0.9,
        datetime(2026, 1, i, tzinfo=UTC),
        f"session-{i}",
        "main_user",
        "voyage-4-large",
        1024,
    )


def test_search_page_is_stable_and_has_truthful_lookahead(monkeypatch):
    captured = {}

    def fake_search(text, limit=5):
        captured["text"] = text
        captured["limit"] = limit
        return [_row(i) for i in range(1, limit + 1)]

    monkeypatch.setattr(query, "search", fake_search)
    page = query.search_page("bounded", limit=2, offset=1)

    assert captured == {"text": "bounded", "limit": 4}
    assert [row[0] for row in page.results] == ["content-2", "content-3"]
    assert page.limit == 2
    assert page.offset == 1
    assert page.has_more is True
    assert page.next_offset == 3


def test_search_page_does_not_advertise_beyond_result_bound(monkeypatch):
    def fake_search(text, limit=5):
        return [_row((i % 28) + 1) for i in range(min(limit, query.MAX_SEARCH_LIMIT))]

    monkeypatch.setattr(query, "search", fake_search)
    page = query.search_page("bounded", limit=20, offset=query.MAX_OFFSET)

    assert page.has_more is False
    assert page.next_offset is None


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "invalid_query"),
        ("", "invalid_query"),
        ("x" * (query.MAX_QUERY_CHARS + 1), "query_too_long"),
    ],
)
def test_query_text_is_bounded(value, code):
    with pytest.raises(query.QueryError) as exc:
        query._validate_query_text(value)
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "invalid_limit"),
        (0, "invalid_limit"),
        ("5", "invalid_limit"),
    ],
)
def test_search_limit_is_typed_and_positive(value, code):
    with pytest.raises(query.QueryError) as exc:
        query._normalise_search_args(value)
    assert exc.value.code == code


def test_search_offset_has_resource_bound():
    with pytest.raises(query.QueryError) as exc:
        query._normalise_search_args(5, query.MAX_OFFSET + 1)
    assert exc.value.code == "offset_out_of_bounds"


def test_embedding_vector_requires_finite_configured_dimension():
    assert len(query._validate_embedding_vector([0.0] * 1024)) == 1024
    with pytest.raises(query.QueryError, match="dimension") as exc:
        query._validate_embedding_vector([0.0] * 8)
    assert exc.value.code == "embedding_dimension_mismatch"
    with pytest.raises(query.QueryError) as exc:
        query._validate_embedding_vector([float("nan")] + [0.0] * 1023)
    assert exc.value.code == "invalid_embedding"


def test_like_escape_protects_wildcards_and_escape_character():
    assert query._escape_like("100%_done\\") == "100\\%\\_done\\\\"


def test_embedding_response_model_mismatch_fails_closed(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "other-model", "data": [{"embedding": [0.0] * 1024}]}

    monkeypatch.setattr(query, "_read_voyage_key", lambda: "test-key")
    monkeypatch.setattr(query.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(query.QueryError) as exc:
        query.embed_query("model mismatch")
    assert exc.value.code == "embedding_model_mismatch"


def test_get_db_marks_connection_read_only(monkeypatch):
    class Connection:
        def __init__(self):
            self.readonly = None

        def set_session(self, *, readonly):
            self.readonly = readonly

        def close(self):
            pass

    conn = Connection()
    monkeypatch.setattr(query, "DATABASE_DSN", "postgresql://synthetic")
    monkeypatch.setattr(query.psycopg2, "connect", lambda *args, **kwargs: conn)
    assert query.get_db() is conn
    assert conn.readonly is True


def test_search_rechecks_provider_dimension_before_opening_database(monkeypatch):
    monkeypatch.setattr(query, "embed_query", lambda text: [0.0] * 8)
    monkeypatch.setattr(query, "get_db", lambda: pytest.fail("database must not be opened"))
    with pytest.raises(query.QueryError) as exc:
        query.search("bad-vector")
    assert exc.value.code == "embedding_dimension_mismatch"


def test_sql_search_filters_model_and_dimension_and_returns_provenance(monkeypatch):
    class Cursor:
        def __init__(self):
            self.calls = []
            self.rows = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "embedding <=>" in sql:
                self.rows = [
                    (
                        "row-1",
                        "compatible content",
                        1.0,
                        datetime(2026, 1, 1, tzinfo=UTC),
                        "session-1",
                        "main_user",
                        "[0,0]",
                        datetime(2026, 1, 1, tzinfo=UTC),
                        "voyage-4-large",
                        1024,
                    )
                ]
            else:
                self.rows = []

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.cursor_obj = Cursor()

        def cursor(self):
            return self.cursor_obj

        def rollback(self):
            pass

        def close(self):
            pass

    conn = Connection()
    monkeypatch.setattr(query, "embed_query", lambda text: [0.0] * 1024)
    monkeypatch.setattr(query, "get_db", lambda: conn)
    rows = query.search("compatible", limit=1)

    assert rows[0][5:] == ("voyage-4-large", 1024)
    vector_sql, params = next((sql, params) for sql, params in conn.cursor_obj.calls if "embedding <=>" in sql)
    assert "embedding_model = %s" in vector_sql
    assert "vector_dims(embedding) = %s" in vector_sql
    assert params[1:3] == ("voyage-4-large", 1024)


def test_mcp_pagination_and_provenance_are_structured(monkeypatch):
    monkeypatch.setattr(
        query,
        "search_page",
        lambda text, *, limit, offset: query.SearchPage(
            results=[_row()], limit=limit, offset=offset, has_more=True, next_offset=offset + limit
        ),
    )
    result = mcp_server.run_search("bounded", limit=4, offset=2)

    assert result["count"] == 1
    assert result["limit"] == 4
    assert result["offset"] == 2
    assert result["has_more"] is True
    assert result["next_offset"] == 6
    item = result["results"][0]
    assert item["embedding_model"] == "voyage-4-large"
    assert item["embedding_dimension"] == 1024
    assert item["provenance"]["session_id"] == "session-1"
    assert item["provenance"]["timestamp"].startswith("2026-01-01")


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"query_text": 123}, "invalid_query"),
        ({"query_text": "ok", "limit": -1}, "invalid_limit"),
        ({"query_text": "ok", "limit": 1, "offset": -1}, "offset_out_of_bounds"),
        ({"query_text": "ok", "limit": 1, "offset": query.MAX_OFFSET + 1}, "offset_out_of_bounds"),
    ],
)
def test_mcp_rejects_hostile_or_unbounded_inputs(kwargs, code, monkeypatch):
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid input must not reach the search backend")

    monkeypatch.setattr(query, "search_page", fail)
    result = mcp_server.run_search(**kwargs)
    assert result == {"error": {"code": code}}
    assert called is False


def test_mcp_rejects_overlong_query_before_backend(monkeypatch):
    monkeypatch.setattr(query, "search_page", lambda *args, **kwargs: pytest.fail("backend must not run"))
    result = mcp_server.run_search("x" * (query.MAX_QUERY_CHARS + 1))
    assert result == {"error": {"code": "query_too_long"}}


def test_mcp_exposes_only_read_only_search_tool():
    tools = [tool.name for tool in __import__("asyncio").run(mcp_server.build_server().list_tools())]
    assert tools == ["search"]


def test_mcp_failure_never_echoes_backend_text(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@example.invalid/db")

    monkeypatch.setattr(query, "search_page", fail)
    result = mcp_server.run_search("safe")
    assert result == {"error": {"code": "search_failed", "type": "RuntimeError"}}
    assert "secret" not in repr(result)


def test_mcp_malformed_backend_row_is_structured(monkeypatch):
    monkeypatch.setattr(
        query,
        "search_page",
        lambda *args, **kwargs: query.SearchPage(
            results=[("too-short",)], limit=5, offset=0, has_more=False, next_offset=None
        ),
    )
    result = mcp_server.run_search("safe")
    assert result == {"error": {"code": "search_failed", "type": "IndexError"}}


def _explain_row(i: int = 1):
    return {
        "content": f"content-{i}",
        "score": 0.9,
        "timestamp": datetime(2026, 1, i, tzinfo=UTC),
        "session_id": f"session-{i}",
        "source_type": "main_user",
        "embedding_model": "voyage-4-large",
        "embedding_dimension": 1024,
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


def test_search_page_explain_keeps_default_and_pagination(monkeypatch):
    """Phase 4F1 slice2 genuine red (task #39).

    ``query.search_page(..., explain=True)`` must return the same runtime
    ``SearchPage`` type whose results are slice1-shaped structured dict rows
    (each carrying the frozen explain literal), while the default
    ``search_page`` (no explain) keeps returning tuple rows with identical
    type/value/item order, and the pagination fields (limit/offset/has_more/
    next_offset) plus the real look-ahead remain truthful in both paths.

    The seam is the public ``query.search_page``; ``query.search`` is stubbed
    to observe whether ``explain`` is forwarded and to return
    ``offset+limit+1`` slice1-shaped dict rows.  No private helper/trace/PG.

    On the current head this node fails ONLY because the public
    ``search_page`` does not accept the ``explain`` keyword argument.
    """
    captured = {}

    def fake_search(text, limit=5, *, explain=False):
        captured["explain"] = explain
        return [_explain_row(i) for i in range(1, limit + 1)]

    monkeypatch.setattr(query, "search", fake_search)

    default_before = query.search_page("explain", limit=2, offset=1)
    explained = query.search_page("explain", limit=2, offset=1, explain=True)
    default_after = query.search_page("explain", limit=2, offset=1)

    # Default path: identical runtime type, tuple rows, item order.
    assert type(default_after) is type(default_before)
    assert default_after.results == default_before.results
    assert isinstance(default_after.results, list) and all(isinstance(r, tuple) for r in default_after.results)

    # Explained path: same SearchPage runtime type, dict rows with frozen literal.
    assert type(explained) is type(default_before)
    assert explained.results[0]["content"] == "content-2"
    assert explained.results[0]["explain"] == _explain_row()["explain"]

    # explain forwarded to the underlying public search.
    assert captured["explain"] is True

    # Pagination identical and truthful (look-ahead still real).
    for field in ("limit", "offset", "has_more", "next_offset"):
        assert getattr(explained, field) == getattr(default_before, field), field
    assert explained.has_more is True
    assert explained.next_offset == 3
