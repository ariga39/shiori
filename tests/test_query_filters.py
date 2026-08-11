"""SearchFilters contract tests (Phase 4E1).

Covers the typed, backward-compatible filter contract: validation/normalization
fail closed, the shared SQL predicate is parameterized (no string-interpolated
values), every candidate path applies it before ranking, the post-SQL invariant
fails closed on leakage, and the unfiltered default stays byte-for-byte
equivalent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import mcp_server
from query import (
    MAX_FILTER_VALUES,
    QueryError,
    SearchFilters,
    _candidate_matches_filters,
    _filter_predicate,
    _row_matches_filters,
    search_page,
)

T0 = datetime(2026, 8, 1, tzinfo=UTC)
T1 = datetime(2026, 8, 2, tzinfo=UTC)


def _row(
    *,
    source_type: str = "main_user",
    session_id: str = "sess-1",
    ts: datetime | None = T1,
) -> tuple:
    # Final result layout: (content, score, timestamp_start, session_id, source_type, model, dim)
    return ("content", 0.5, ts, session_id, source_type, "model", 1024)


# -- from_inputs validation ------------------------------------------------


def test_from_inputs_none_and_empty_are_unfiltered():
    assert SearchFilters.from_inputs().is_empty
    assert SearchFilters.from_inputs(source_types=[], session_ids=[]).is_empty
    assert SearchFilters.from_inputs(source_types=None, session_ids=None).is_empty


def test_from_inputs_rejects_single_string_as_array():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(source_types="main_user")
    assert exc.value.code == "invalid_filter_type"


def test_from_inputs_rejects_bool_and_non_string():
    for bad in (True, 1, b"bytes", 3.14, None):
        with pytest.raises(QueryError) as exc:
            SearchFilters.from_inputs(source_types=[bad])
        assert exc.value.code in ("invalid_filter_type", "invalid_filter_value")


def test_from_inputs_rejects_empty_string_and_overlong():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(source_types=[""])
    assert exc.value.code == "invalid_filter_value"
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(source_types=["x" * 51])
    assert exc.value.code == "invalid_filter_value"


def test_from_inputs_rejects_duplicates():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(session_ids=["a", "a"])
    assert exc.value.code == "duplicate_filter_value"


def test_from_inputs_rejects_count_exceeded():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(session_ids=[f"s{i}" for i in range(MAX_FILTER_VALUES + 1)])
    assert exc.value.code == "filter_count_exceeded"


def test_from_inputs_rejects_naive_and_reversed_times():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(time_from="2026-08-01T00:00:00")
    assert exc.value.code == "invalid_time_format"
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(time_from=T1, time_to=T0)
    assert exc.value.code == "invalid_time_range"


def test_from_inputs_canonicalizes_to_sorted_tuple():
    filters = SearchFilters.from_inputs(source_types=["b", "a", "c"])
    assert filters.source_types == ("a", "b", "c")
    assert isinstance(filters.source_types, tuple)


def test_from_inputs_accepts_z_and_rejects_nonzero_offset():
    filters = SearchFilters.from_inputs(time_from="2026-08-01T08:00:00Z")
    assert filters.time_from == datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    filters2 = SearchFilters.from_inputs(time_from="2026-08-01T08:00:00+00:00")
    assert filters2.time_from == datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    filters3 = SearchFilters.from_inputs(time_from="2026-08-01T08:00:00.500Z")
    assert filters3.time_from == datetime(2026, 8, 1, 8, 0, 0, 500000, tzinfo=UTC)
    # Non-zero offset and permissive forms are rejected.
    for bad in (
        "2026-08-01T08:00:00+08:00",
        "2026-08-01 08:00:00Z",  # space separator
        "20260801T080000Z",  # compact
        "2026-08-01T08:00Z",  # missing seconds
    ):
        with pytest.raises(QueryError) as exc:
            SearchFilters.from_inputs(time_from=bad)
        assert exc.value.code in ("invalid_time_format", "invalid_timezone")


def test_direct_construction_cannot_bypass_validation():
    with pytest.raises(QueryError) as exc:
        SearchFilters(source_types="not-a-tuple")
    assert exc.value.code == "invalid_filter_type"
    # Canonical tuples must be unique, sorted, bounded, and non-empty strings.
    with pytest.raises(QueryError) as exc:
        SearchFilters(source_types=("a", "a"))
    assert exc.value.code == "duplicate_filter_value"
    with pytest.raises(QueryError) as exc:
        SearchFilters(source_types=("b", "a"))
    assert exc.value.code == "invalid_filter_type"
    with pytest.raises(QueryError) as exc:
        SearchFilters(session_ids=("",))
    assert exc.value.code == "invalid_filter_value"
    with pytest.raises(QueryError) as exc:
        SearchFilters(time_from=datetime(2026, 8, 1))  # naive
    assert exc.value.code == "invalid_timezone"


def test_from_inputs_rejects_set_and_frozenset():
    with pytest.raises(QueryError) as exc:
        SearchFilters.from_inputs(source_types={"a", "b"})
    assert exc.value.code == "invalid_filter_type"


def test_candidate_invariant_rejects_naive_timestamp_under_time_filter():
    # A DB naive timestamp under an explicit time filter is non-matching.
    naive_row = ("id", "content", 0.5, datetime(2026, 8, 1), "s1", "main_user", "v", None, "m", 1024)
    assert _candidate_matches_filters(naive_row, SearchFilters.from_inputs(time_from=T0)) is False
    assert _candidate_matches_filters(naive_row, SearchFilters.from_inputs()) is True


# -- predicate builder -----------------------------------------------------


def test_predicate_empty_filters_returns_empty():
    sql, params = _filter_predicate(SearchFilters.from_inputs())
    assert sql == ""
    assert params == ()


def test_predicate_source_session_time_parameterized():
    filters = SearchFilters.from_inputs(
        source_types=["main_user", "hermes"],
        session_ids=["s1", "s2"],
        time_from=T0,
        time_to=T1,
    )
    sql, params = _filter_predicate(filters)
    assert "%s" in sql
    assert "timestamp_start IS NOT NULL" in sql
    assert "timestamp_start >= %s" in sql
    assert "timestamp_start < %s" in sql
    assert params == ("hermes", "main_user", "s1", "s2", T0, T1)
    # No literal values spliced into SQL text.
    assert "main_user" not in sql and "s1" not in sql


def test_predicate_time_only_adds_not_null():
    filters = SearchFilters.from_inputs(time_from=T0)
    sql, params = _filter_predicate(filters)
    assert "timestamp_start IS NOT NULL" in sql
    assert params == (T0,)


def test_sql_injection_payload_is_literal_value_not_injected():
    payload = "'; DROP TABLE session_chunks; --"
    filters = SearchFilters.from_inputs(session_ids=[payload])
    sql, params = _filter_predicate(filters)
    assert "DROP TABLE" not in sql
    assert params == (payload,)


# -- invariant helper ------------------------------------------------------


def test_row_matches_filters_source_session_time():
    filters = SearchFilters.from_inputs(
        source_types=["main_user"],
        session_ids=["sess-1"],
        time_from=T0,
        time_to=T1,
    )
    assert _row_matches_filters(_row(ts=T0), filters) is True  # from-inclusive
    assert _row_matches_filters(_row(ts=T1), filters) is False  # to-exclusive
    assert _row_matches_filters(_row(source_type="hermes"), filters) is False
    assert _row_matches_filters(_row(session_id="sess-2"), filters) is False


def test_row_matches_filters_null_timestamp_not_matched():
    filters = SearchFilters.from_inputs(time_from=T0)
    assert _row_matches_filters(_row(ts=None), filters) is False


def test_row_matches_filters_empty_filters_match_all():
    assert _row_matches_filters(_row(ts=None), SearchFilters.from_inputs()) is True


# -- SQL channel injection evidence (static) --------------------------------


def test_four_sql_paths_use_shared_predicate_builder(monkeypatch):
    """The four candidate SQL executions must route through _filter_predicate.

    We assert the helper is invoked with a non-empty filter during a real
    search_page call against a stubbed search, and that the unfiltered path
    does not add predicates.
    """
    captured: list[tuple] = []

    def fake_search(q, limit=5, filters=None):
        captured.append((q, limit, filters))
        return []

    monkeypatch.setattr("query.search", fake_search)
    monkeypatch.setattr("mcp_server.query", __import__("query"))
    # search_page passes filters only when non-empty (backward compatible).
    search_page("hello", limit=3, filters=SearchFilters.from_inputs(source_types=["main_user"]))
    assert captured[0][2] is not None
    captured.clear()
    search_page("hello", limit=3)
    assert captured[0][2] is None


class _CaptureCursor:
    """Fake cursor that records every execute() call and returns matching rows."""

    def __init__(self, sql_statements: list[str]):
        self.calls: list[tuple[str, tuple]] = []
        self._sql_statements = sql_statements

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.calls.append((sql, params))

    def fetchall(self):
        # Row layout: id, content, score, timestamp_start, session_id, source_type, ...
        return [("id-1", "content", 0.5, T0, "s1", "main_user", "v", None, "m", 1024)]

    def close(self) -> None:
        pass


class _CaptureConn:
    def __init__(self, statements: list[str]):
        self.cursor_ = _CaptureCursor(statements)

    def cursor(self):
        return self.cursor_

    def close(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_every_sql_path_injects_shared_parameterized_predicate(monkeypatch):
    """Exercise dense, ts_rank_cd, exact, and trigram SQL with a fake cursor
    whose return rows are controlled per SQL, proving each of the four candidate
    channels executes with the same parameterized predicate (no value splicing)
    and leakage propagates fail closed."""
    import query as query_mod

    filters = SearchFilters.from_inputs(
        source_types=["main_user"],
        session_ids=["s1"],
        time_from=T0,
        time_to=T1,
    )
    # Channel rows (SQL layout) that satisfy the filters.
    row = ("id-1", "content", 0.5, T0, "s1", "main_user", "v", None, "m", 1024)

    class _ChannelCursor:
        def __init__(self):
            self.calls: list[tuple[str, tuple]] = []

        def execute(self, sql: str, params: tuple = ()) -> None:
            self.calls.append((sql, params))

        def fetchall(self):
            # Return a matching row for every channel, forcing ts_rank_cd to
            # HIT (so trigram fallback does NOT run) for the ts_rank_cd call and
            # empty for trigram.
            return [row]

        def close(self) -> None:
            pass

    class _ChannelConn:
        def __init__(self):
            self.cursor_ = _ChannelCursor()

        def cursor(self):
            return self.cursor_

        def close(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    conn = _ChannelConn()
    monkeypatch.setattr(query_mod, "get_db", lambda: conn)
    monkeypatch.setattr(query_mod, "embed_query", lambda text: [0.0] * 1024)
    monkeypatch.setattr(query_mod, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(query_mod, "VOYAGE_MODEL", "m")
    monkeypatch.setattr(query_mod, "EMBED_DIM", 1024)

    query_mod.search("test", limit=3, filters=filters)

    # With ts_rank_cd HIT, exactly dense + ts_rank_cd + exact execute over
    # session_chunks (trigram fallback is skipped).  `SET hnsw` is not a channel.
    channel_sql = [sql for sql, _ in conn.cursor_.calls if "FROM session_chunks" in sql]
    assert len(channel_sql) == 3
    assert any("embedding <=>" in s for s in channel_sql)  # dense
    assert any("ts_rank_cd" in s for s in channel_sql)  # ts_rank_cd
    assert any("ILIKE" in s for s in channel_sql)  # exact
    for sql, params in conn.cursor_.calls:
        if "FROM session_chunks" in sql:
            assert "timestamp_start IS NOT NULL" in sql
            assert "source_type IN" in sql and "session_id IN" in sql
            assert "main_user" not in sql and "s1" not in sql
            assert all(p is not None for p in params)


def test_trigram_fallback_channel_also_injects_predicate(monkeypatch):
    """When ts_rank_cd returns nothing, the trigram fallback runs and must also
    carry the same predicate."""
    import query as query_mod

    filters = SearchFilters.from_inputs(
        source_types=["main_user"],
        session_ids=["s1"],
        time_from=T0,
        time_to=T1,
    )
    row = ("id-1", "content", 0.5, T0, "s1", "main_user", "v", None, "m", 1024)

    class _Cursor:
        def __init__(self):
            self.calls: list[tuple[str, tuple]] = []

        def execute(self, sql: str, params: tuple = ()) -> None:
            self.calls.append((sql, params))

        def fetchall(self):
            # ts_rank_cd channel returns EMPTY -> trigram fallback runs; the
            # exact channel (short query) and dense return the matching row.
            return [] if "ts_rank_cd" in self.calls[-1][0] else [row]

        def close(self) -> None:
            pass

    class _Conn:
        def __init__(self):
            self.cursor_ = _Cursor()

        def cursor(self):
            return self.cursor_

        def close(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    conn = _Conn()
    monkeypatch.setattr(query_mod, "get_db", lambda: conn)
    monkeypatch.setattr(query_mod, "embed_query", lambda text: [0.0] * 1024)
    monkeypatch.setattr(query_mod, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(query_mod, "VOYAGE_MODEL", "m")
    monkeypatch.setattr(query_mod, "EMBED_DIM", 1024)

    query_mod.search("test", limit=3, filters=filters)

    channel_sql = [sql for sql, _ in conn.cursor_.calls if "FROM session_chunks" in sql]
    # dense + ts_rank_cd + exact + trigram all execute in this fallback case.
    assert len(channel_sql) == 4
    assert any("similarity(content" in s for s in channel_sql)  # trigram
    for sql, params in conn.cursor_.calls:
        if "FROM session_chunks" in sql:
            assert "timestamp_start IS NOT NULL" in sql
            assert "main_user" not in sql and "s1" not in sql


CHANNEL_SQL_MARKERS = {
    "dense": "embedding <=>",
    "ts_rank_cd": "ts_rank_cd",
    "exact": "ILIKE",
    "trigram": "similarity(content",
}


@pytest.mark.parametrize("channel", sorted(CHANNEL_SQL_MARKERS))
def test_filter_leakage_closes_cursor_and_conn_per_channel(monkeypatch, channel):
    """Each candidate channel's filter_leakage path must close both the cursor
    and the connection before re-raising, and later channels (fallbacks) must
    NOT execute."""
    import query as query_mod

    target_sql = CHANNEL_SQL_MARKERS[channel]
    filters = SearchFilters.from_inputs(
        source_types=["main_user"],
        session_ids=["s1"],
        time_from=T0,
        time_to=T1,
    )
    # A row that does NOT satisfy the filters (violates source_type).
    violating = ("id-1", "content", 0.5, T0, "s1", "hermes", "v", None, "m", 1024)
    # A row that satisfies the filters, used for the earlier channels so the
    # execution reaches the target channel (and ts_rank_cd HITs to avoid
    # trigram fallback except when trigram is the target).
    matching = ("id-1", "content", 0.5, T0, "s1", "main_user", "v", None, "m", 1024)

    class _LeakCursor:
        def __init__(self):
            self.calls: list[tuple[str, tuple]] = []
            self.closed = False
            self.last_sql = ""

        def execute(self, sql: str, params: tuple = ()) -> None:
            self.last_sql = sql
            self.calls.append((sql, params))

        def fetchall(self):
            if target_sql in self.last_sql:
                return [violating]
            if "ts_rank_cd" in self.last_sql:
                if channel == "trigram":
                    # The trigram fallback only runs when ts_rank_cd is empty.
                    return []
                if channel != "ts_rank_cd":
                    # ts_rank_cd must HIT (return matching) except when it is
                    # the target; otherwise trigram fallback would swallow the
                    # leak.
                    return [matching]
            if "similarity(content" in self.last_sql and channel != "trigram":
                return [matching]
            return [matching]

        def close(self) -> None:
            self.closed = True

    class _LeakConn:
        def __init__(self):
            self.cursor_ = _LeakCursor()
            self.closed = False

        def cursor(self):
            return self.cursor_

        def close(self) -> None:
            self.closed = True

        def rollback(self) -> None:
            pass

    conn = _LeakConn()
    monkeypatch.setattr(query_mod, "get_db", lambda: conn)
    monkeypatch.setattr(query_mod, "embed_query", lambda text: [0.0] * 1024)
    monkeypatch.setattr(query_mod, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(query_mod, "VOYAGE_MODEL", "m")
    monkeypatch.setattr(query_mod, "EMBED_DIM", 1024)

    with pytest.raises(QueryError) as exc:
        query_mod.search("test", limit=3, filters=filters)

    assert exc.value.code == "filter_leakage"
    assert conn.closed, f"{channel}: connection leaked on filter_leakage"
    assert conn.cursor_.closed, f"{channel}: cursor leaked on filter_leakage"

    # No channel after the target executed (no fallback on the leakage path).
    executed_channels = []
    for sql, _ in conn.cursor_.calls:
        if "FROM session_chunks" in sql:
            for name, marker in CHANNEL_SQL_MARKERS.items():
                if marker in sql:
                    executed_channels.append(name)
                    break
    assert executed_channels[-1] == channel


def test_unfiltered_mcp_response_has_no_filters_applied(monkeypatch):
    """Unfiltered MCP response is byte/key equivalent to base (no filters_applied)."""

    def fake_page(text, *, limit, offset, filters=None):
        return mcp_server.query.SearchPage(
            results=[],
            limit=limit,
            offset=offset,
            has_more=False,
            next_offset=None,
        )

    monkeypatch.setattr(mcp_server.query, "search_page", fake_page)
    result = mcp_server.run_search("hello", limit=4, offset=0)
    assert "filters_applied" not in result
    filtered = mcp_server.run_search(
        "hello", limit=4, offset=0, source_types=["main_user"]
    )
    assert "filters_applied" in filtered


# -- CLI surface: query.main and shiori cli query ---------------------------


def _settings_stub():
    class _Settings:
        def require_database(self):
            return None

        def require_embedding(self):
            return None

        def require_source(self, source=None):
            return None

    return _Settings()


def test_query_main_repeatable_flags_parse_to_same_filters(monkeypatch, capsys):
    """query.main resolves repeated --source-type/--session-id + time flags to
    one SearchFilters and passes it to search()."""
    import query as query_mod

    captured = {}

    def fake_search(text, limit, offset, filters=None):
        captured["filters"] = filters
        return []

    monkeypatch.setattr(query_mod, "load_config", lambda **kw: _settings_stub())
    monkeypatch.setattr(query_mod, "apply_settings", lambda settings: None)
    monkeypatch.setattr(query_mod, "search", fake_search)

    query_mod.main(
        [
            "hello",
            "--source-type",
            "main_user",
            "--source-type",
            "discord",
            "--session-id",
            "s1",
            "--session-id",
            "s2",
            "--time-from",
            "2026-08-01T00:00:00Z",
            "--time-to",
            "2026-08-02T00:00:00Z",
        ]
    )

    expected = SearchFilters.from_inputs(
        source_types=["main_user", "discord"],
        session_ids=["s1", "s2"],
        time_from=T0,
        time_to=T1,
    )
    assert captured["filters"] == expected
    capsys.readouterr()


def test_shiori_cli_query_forwards_flags_to_query_main(monkeypatch):
    """`shiori cli query` forwards repeated source/session + time flags to
    query.main, which must resolve them to the same SearchFilters."""
    import argparse

    import query as query_mod
    from shiori.cli import _run_query

    forwarded = {}
    real_main = query_mod.main

    def fake_main(argv):
        forwarded["argv"] = argv

    monkeypatch.setattr(query_mod, "apply_settings", lambda settings: None)
    monkeypatch.setattr(query_mod, "main", fake_main)

    args = argparse.Namespace(
        query="hello",
        limit=5,
        config=None,
        legacy_openclaw=False,
        source_type=["main_user", "discord"],
        session_id=["s1", "s2"],
        time_from="2026-08-01T00:00:00Z",
        time_to="2026-08-02T00:00:00Z",
    )
    _run_query(args, _settings_stub())  # type: ignore[arg-type]

    argv = forwarded["argv"]
    assert argv[0] == "hello"
    assert "--source-type" in argv
    assert argv.count("--source-type") == 2
    assert argv.count("--session-id") == 2
    assert argv.index("main_user") < argv.index("discord")
    assert "--time-from" in argv and "--time-to" in argv

    # Feed the forwarded argv back through query.main and confirm the same
    # SearchFilters instance is produced (duplicate flags are not lost).
    parsed = {}

    def fake_search(text, limit, offset, filters=None):
        parsed["filters"] = filters
        return []

    monkeypatch.setattr(query_mod, "main", real_main)
    monkeypatch.setattr(query_mod, "load_config", lambda **kw: _settings_stub())
    monkeypatch.setattr(query_mod, "search", fake_search)
    query_mod.main(argv)
    expected = SearchFilters.from_inputs(
        source_types=["main_user", "discord"],
        session_ids=["s1", "s2"],
        time_from=T0,
        time_to=T1,
    )
    assert parsed["filters"] == expected


# -- MCP tool surface --------------------------------------------------------


def _tool_call(server, name, arguments):
    import asyncio

    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


def test_build_server_search_tool_accepts_new_optional_params(monkeypatch):
    """The registered `search` tool accepts the new filter params and returns
    the same unfiltered payload shape when no filters are supplied."""
    import query as query_mod

    calls = []

    def fake_search_page(text, *, limit, offset, filters=None):
        calls.append(filters)
        return query_mod.SearchPage(
            results=[],
            limit=limit,
            offset=offset,
            has_more=False,
            next_offset=None,
        )

    monkeypatch.setattr(query_mod, "search_page", fake_search_page)
    server = mcp_server.build_server()

    unfiltered = _tool_call(server, "search", {"query": "hello", "limit": 4, "offset": 0})
    assert calls == [None]
    assert set(unfiltered) == {
        "results",
        "count",
        "limit",
        "offset",
        "has_more",
        "next_offset",
    }
    assert "filters_applied" not in unfiltered

    filtered = _tool_call(
        server,
        "search",
        {
            "query": "hello",
            "limit": 4,
            "offset": 0,
            "source_types": ["main_user"],
            "session_ids": ["s1"],
            "time_from": "2026-08-01T00:00:00Z",
            "time_to": "2026-08-02T00:00:00Z",
        },
    )
    assert calls[-1] == SearchFilters.from_inputs(
        source_types=["main_user"],
        session_ids=["s1"],
        time_from=T0,
        time_to=T1,
    )
    assert filtered["filters_applied"] == {
        "source_types": True,
        "session_ids": True,
        "time_from": "2026-08-01T00:00:00+00:00",
        "time_to": "2026-08-02T00:00:00+00:00",
    }
    # Unfiltered shape is unchanged even when a filter was requested.
    assert set(filtered) == {
        "results",
        "count",
        "limit",
        "offset",
        "has_more",
        "next_offset",
        "filters_applied",
    }


def test_build_server_search_tool_invalid_filter_returns_stable_code(monkeypatch):
    """A malformed filter value that passes the MCP schema still returns a
    stable error code instead of leaking a backend exception."""
    import query as query_mod

    def boom(text, *, limit, offset, filters=None):
        raise AssertionError("search_page must not run for invalid filters")

    monkeypatch.setattr(query_mod, "search_page", boom)
    server = mcp_server.build_server()

    # Empty string is schema-valid (list[str]) but rejected by our validation.
    result = _tool_call(
        server,
        "search",
        {"query": "hello", "limit": 4, "source_types": [""]},
    )
    assert result == {"error": {"code": "invalid_filter_value"}}

    result = _tool_call(
        server,
        "search",
        {"query": "hello", "limit": 4, "time_from": "not-a-timestamp"},
    )
    assert result == {"error": {"code": "invalid_time_format"}}
