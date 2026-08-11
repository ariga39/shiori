from datetime import UTC, datetime, timedelta

import psycopg2
import pytest

import query
from query import SearchFilters

# Fixed unit vector along the first axis; query embedding is mocked to this.
QUERY_EMB = [1.0] + [0.0] * 1023
# Unit vector 30 degrees off QUERY_EMB (cos = 0.8660).
NEAR_EMB = [0.8660254] + [0.5] + [0.0] * 1022
# Unit vector 60 degrees off QUERY_EMB (cos = 0.5).
FAR_EMB = [0.5] + [0.8660254] + [0.0] * 1022


def _insert(conn, sid, content, emb, ts, src="main_user", channel=None, created_at=None):
    cur = conn.cursor()
    if channel is None:
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large",
             ts, ts, 0, 0, content, created_at),
        )
    else:
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                channel, content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large",
             ts, ts, 0, 0, channel, content, created_at),
        )
    conn.commit()
    cur.close()


@pytest.fixture
def mock_embed(monkeypatch):
    monkeypatch.setattr(query, "embed_query", lambda q: QUERY_EMB)


def test_search_returns_vector_matched_content(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-vec"
    _insert(conn, sid, "shiori_test_alpha primary", QUERY_EMB, datetime.now(UTC))
    _insert(conn, sid, "shiori_test_beta secondary", FAR_EMB, datetime.now(UTC))
    res = query.search("shiori_test_alpha", limit=20)
    mine = [r for r in res if r[3] == sid]
    assert mine, "expected test chunk in results"
    assert "shiori_test_alpha" in mine[0][0]


def test_temporal_decay_ranks_recent_higher(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-decay"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    # Old chunk has the HIGHEST raw vector score (QUERY_EMB, cos=1.0) but is very
    # old; recent chunk has a lower raw vector score (FAR_EMB, cos=0.5 to the
    # query axis; mutual cosine with old = 0.5 < 0.85 so MMR won't dedupe). Decay
    # must reverse their order: old 1.0 * 2^-4 = 0.0625 < recent 0.5 * 1.0.
    _insert(conn, sid, "shiori_test_old_item", QUERY_EMB, old)
    _insert(conn, sid, "shiori_test_new_item", FAR_EMB, now)
    # Explicit temporal intent (Phase 4E2): a structured lower bound covering
    # both rows keeps the original 30-day decay active for this query.
    filters = SearchFilters.from_inputs(
        session_ids=[sid], time_from=old - timedelta(days=1)
    )
    res = query.search("zzqx no-bm25-match 9", limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2
    contents = [r[0] for r in mine]
    assert contents.index("shiori_test_new_item") < contents.index("shiori_test_old_item")


def test_null_ts_uses_created_at_for_decay(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-nullts"
    now = datetime.now(UTC)
    old = now - timedelta(days=60)
    # Recent chunk with a real ts (FAR_EMB, cos=0.5) → score 0.5.
    _insert(conn, sid, "shiori_test_recent_x", FAR_EMB, now)
    # Chunk with NULL ts but OLD created_at (QUERY_EMB, cos=1.0). Without decay
    # it would score a flat 1.0 and outrank the recent chunk; with created_at
    # fallback it decays by created_at: 1.0 * 2^-2 = 0.25 < 0.5.
    _insert(conn, sid, "shiori_test_null_old_x", QUERY_EMB, None, created_at=old)
    # Explicit recency intent via a standalone `latest` token (Phase 4E2): the
    # original decay (with created_at fallback for the NULL-ts row) stays
    # active.  Regression maintenance of the pre-4E2 fallback contract, not a
    # new TDD red.
    res = query.search("latest zzqx no-bm25-match 5", limit=300)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2
    contents = [r[0] for r in mine]
    assert contents.index("shiori_test_recent_x") < contents.index("shiori_test_null_old_x")


def test_mmr_keeps_distinct_content_despite_identical_embeddings(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-mmr"
    now = datetime.now(UTC)
    # Two byte-different chunks in the same session with IDENTICAL embeddings.
    # The provenance-aware contract keeps both: distinct content is separate
    # evidence regardless of embedding similarity.
    _insert(conn, sid, "shiori_test_dup_a", QUERY_EMB, now)
    _insert(conn, sid, "shiori_test_dup_b", QUERY_EMB, now)
    res = query.search("zzqx no-bm25-match 7", limit=20)
    mine = [r[0] for r in res if r[3] == sid]
    assert set(mine) == {"shiori_test_dup_a", "shiori_test_dup_b"}


def test_tsvector_bm25_match_ranks_first(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-bm25"
    now = datetime.now(UTC)
    # Both chunks embed far from the query vector, so pure vector search would
    # rank neither on top. Only the chunk whose text contains the query term
    # gets a BM25/trigram boost.
    _insert(conn, sid, "explicit snowflake keyword alpha", FAR_EMB, now)
    _insert(conn, sid, "unrelated random chatter bravo", FAR_EMB, now)
    res = query.search("snowflake", limit=20)
    mine = [r for r in res if r[3] == sid]
    assert mine
    assert "snowflake" in mine[0][0]


# ── Cycle 5 fixes ───────────────────────────────────────────────────────────
#
# These force a *server-side* Postgres error (by running a genuinely-failing SET
# against the real connection) so the transaction is left aborted — exactly the
# failure the fixes are meant to survive — rather than raising a Python-side
# exception that would not abort the transaction.

class _FailCursor:
    """Wraps a real cursor; aborts the server transaction on statements whose
    SQL contains any of `fail_substrings`, then raises. Delegates everything
    else to the real cursor so the query still hits the real DB."""

    def __init__(self, real, fail_substrings):
        self._real = real
        self._fail = fail_substrings

    def execute(self, sql, params=None):
        if isinstance(sql, str) and any(s in sql for s in self._fail):
            try:
                self._real.execute("SET totally.bogus.guc = 1")
            except Exception:
                pass
            raise psycopg2.Error("forced statement failure (server-side)")
        return self._real.execute(sql, params)

    def fetchall(self):
        return self._real.fetchall()

    def close(self):
        return self._real.close()


class _FailConn:
    """Wraps a real connection whose cursors fail on the given statements."""

    def __init__(self, real, fail_substrings):
        self._real = real
        self._fail = fail_substrings

    def cursor(self):
        return _FailCursor(self._real.cursor(), self._fail)

    def rollback(self):
        return self._real.rollback()

    def close(self):
        # Deliberately a no-op: the wrapped connection is owned by the `db`
        # fixture, which must be able to roll back + clean up + close it after
        # the search has run. Forwarding close here would break fixture teardown.
        pass


def test_search_survives_set_failure(db, mock_embed, monkeypatch):
    conn, prefix = db
    sid = prefix + "-setfail"
    _insert(conn, sid, "shiori_test_setfail target", QUERY_EMB, datetime.now(UTC))
    monkeypatch.setattr(query, "get_db",
                        lambda: _FailConn(conn, ["SET hnsw.ef_search"]))
    res = query.search("shiori_test_setfail", limit=20)
    mine = [r for r in res if r[3] == sid]
    assert mine, "search must return vector results even when SET fails"
    assert "shiori_test_setfail" in mine[0][0]


def test_bm25_fallback_survives_tsvector_failure(db, mock_embed, monkeypatch):
    conn, prefix = db
    sid = prefix + "-tsvfail"
    now = datetime.now(UTC)
    # Matched chunk is FAR from the query vector (cos 0.5); the other is the
    # closest possible (cos 1.0). Without a working keyword fallback the matched
    # chunk ranks below the other; a working trigram fallback flips it on top.
    _insert(conn, sid, "shiori_test_match snowflake", FAR_EMB, now)
    _insert(conn, sid, "shiori_test_other", QUERY_EMB, now)
    monkeypatch.setattr(query, "get_db",
                        lambda: _FailConn(conn, ["ts_rank_cd"]))
    res = query.search("snowflake", limit=300)
    mine = [r for r in res if r[3] == sid]
    contents = [r[0] for r in mine]
    assert contents.index("shiori_test_match snowflake") < contents.index("shiori_test_other"), \
        "trigram fallback must still return results after a tsvector failure"


def _insert_double_null(conn, sid, content, emb):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,NULL,NULL,0,0,to_tsvector('simple',%s),NULL)""",
        (sid, "main_user", content, str(emb), "voyage-4-large", content),
    )
    conn.commit()
    cur.close()


def test_double_null_uses_null_ts_prior(db, mock_embed):
    conn, prefix = db
    sid = prefix + "-dnull"
    now = datetime.now(UTC)
    # Both chunks match the nonsense keyword "zzqxmarker" via tsvector (so both
    # get a solid BM25 RRF boost into the top-N regardless of the ~20k-row DB),
    # and both are the nearest vector matches. The recent chunk (FAR_EMB) decays
    # ~1.0; the double-NULL chunk (QUERY_EMB, cos 1.0, no ts AND no created_at)
    # must be scaled by NULL_TS_PRIOR (0.25) so it ranks BELOW the recent chunk.
    # Without the prior it would have the higher pre-decay score and rank above.
    _insert(conn, sid, "shiori_test_dn_recent zzqxmarker", FAR_EMB, now)
    _insert_double_null(conn, sid, "shiori_test_dn_null zzqxmarker", QUERY_EMB)
    # Explicit recency intent via a standalone `latest` token (Phase 4E2): the
    # original decay + NULL_TS_PRIOR for the double-NULL row stays active.
    # Regression maintenance of the pre-4E2 fallback contract, not a new TDD
    # red.
    res = query.search("latest zzqxmarker", limit=300)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2
    contents = [r[0] for r in mine]
    assert contents.index("shiori_test_dn_recent zzqxmarker") < contents.index("shiori_test_dn_null zzqxmarker")


# ── Cycle 6: ef_search clamp (pgvector preload now active) ─────────────────
#
# Now that shared_preload_libraries='vector' is configured (2026-08-03), the
# hnsw.ef_search GUC is registered at startup. SET on a legal value takes effect
# immediately; SET out of range (> 1000) errors. These tests observe the value
# actually applied on the live session via SHOW hnsw.ef_search, after running
# search() with a real connection.

def test_ef_search_clamped_to_1000_for_large_limit(db, mock_embed, monkeypatch):
    conn, prefix = db
    sid = prefix + "-efclamp"
    _insert(conn, sid, "shiori_test_efclamp target", QUERY_EMB, datetime.now(UTC))
    # Reuse _FailConn with no failing substrings: it forwards to the real DB but
    # its close() is a no-op, so the fixture still owns `conn` for inspection.
    monkeypatch.setattr(query, "get_db", lambda: _FailConn(conn, []))
    # limit=250 -> pool=1250, which must be clamped to 1000 (not SET as 1250).
    res = query.search("shiori_test_efclamp", limit=250)
    mine = [r for r in res if r[3] == sid]
    assert mine, "search with limit>200 must still return results (no rollback to ef=40)"
    cur = conn.cursor()
    cur.execute("SHOW hnsw.ef_search")
    ef = int(cur.fetchone()[0])
    assert ef == 1000, f"ef_search must be clamped to 1000, got {ef}"


def test_ef_search_equals_pool_for_small_limit(db, mock_embed, monkeypatch):
    conn, prefix = db
    sid = prefix + "-efsmall"
    _insert(conn, sid, "shiori_test_efsmall target", QUERY_EMB, datetime.now(UTC))
    monkeypatch.setattr(query, "get_db", lambda: _FailConn(conn, []))
    # limit=45 -> pool=225 (>= 200 floor, < 1000 cap) -> ef_search stays = pool.
    res = query.search("shiori_test_efsmall", limit=45)
    mine = [r for r in res if r[3] == sid]
    assert mine
    cur = conn.cursor()
    cur.execute("SHOW hnsw.ef_search")
    ef = int(cur.fetchone()[0])
    assert ef == 225, f"expected ef_search=225 (pool), got {ef}"


# ── Cycle 6: real-GUC proof (B-C6-02) ───────────────────────────────────────
#
# These two tests guard against a *regression to false-green*. When pgvector is
# NOT preloaded (shared_preload_libraries missing 'vector'), the extension loads
# lazily and a `SET hnsw.ef_search` issued as the session's first statement is
# silently dropped as a custom placeholder: the SET succeeds and SHOW still
# reports 40, so the clamp tests alone cannot tell a real GUC from a placeholder.
#
# Test 1 asserts a server-side error on an out-of-range value (proves the GUC is
# genuinely registered); Test 2 asserts the GUC row exists in pg_settings. Both
# must PASS today because the live container preloads 'vector' (2026-08-03). If a
# future rebuild loses the preload, these turn red — that is the point.

def test_set_ef_search_out_of_range_raises(db):
    """A truly-registered hnsw.ef_search rejects 1001 with InvalidParameterValue."""
    conn, _ = db
    cur = conn.cursor()
    with pytest.raises(psycopg2.errors.InvalidParameterValue):
        cur.execute("SET hnsw.ef_search = 1001")
    # Abort the failed transaction so teardown cleanup can run on the fixture conn.
    conn.rollback()


def test_ef_search_registered_in_pg_settings(db):
    """hnsw.ef_search must be a real GUC row, not a silent custom placeholder."""
    conn, _ = db
    cur = conn.cursor()
    cur.execute("SELECT name FROM pg_settings WHERE name = 'hnsw.ef_search'")
    assert cur.fetchone() is not None, \
        "hnsw.ef_search must appear in pg_settings; preload of 'vector' is missing"


# ── Short entity/name recall (日和-type) ─────────────────────────────────────
# Regression: short CJK queries (2-4 chars) scored terribly under vector+BM25 —
# tsquery splits CJK into single chars AND-joined ('日' & '和'), both
# high-frequency, so ts_rank is noise; pg_trgm is useless for 2-char strings.
# The ILIKE exact-substring channel fixed it. This test guards that channel.

def test_short_name_exact_substring_recalled(db, mock_embed):
    """A 2-char CJK name must surface via the exact-substring channel even
    when its vector/BM25 signals are weak (mocked to FAR/different)."""
    conn, prefix = db
    sid = prefix + "-shortname"
    now = datetime.now(UTC)
    # Content containing the entity name, but vector-distant from the query emb.
    _insert(conn, sid, "日和拿到权限后开了 HF_XET 第三次尝试,跑得很顺", FAR_EMB, now)
    # A decoy that is vector-close but does NOT contain the name.
    _insert(conn, sid, "shiori_test_alpha primary unrelated topic", QUERY_EMB, now)

    res = query.search("日和", limit=10)
    mine = [r for r in res if r[3] == sid]
    assert mine, "expected short-name test chunk in results"
    assert "日和" in mine[0][0], \
        "exact-substring channel must rank the name-bearing chunk first"

def test_short_name_escapes_like_wildcards(db, mock_embed):
    """ILIKE wildcards in the query must be escaped, not treated as patterns."""
    conn, prefix = db
    sid = prefix + "-escape"
    now = datetime.now(UTC)
    _insert(conn, sid, "进度 100%_done 记录", QUERY_EMB, now)
    # '%' as a query should only match literal percent, not act as wildcard.
    res = query.search("100%", limit=10)
    mine = [r for r in res if r[3] == sid]
    assert mine and "100%" in mine[0][0]
