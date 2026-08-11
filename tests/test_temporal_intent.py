"""Phase 4E2 — intent-gated temporal decay.

Contract: an ORDINARY fact/history query (no explicit temporal intent) must NOT
apply the 30-day temporal decay; an older but clearly more relevant fact must
outrank a newer but weaker match.  Under EXPLICIT structured time bounds the
original 30-day decay still applies (characterization/regression, not a new
red).

All assertions go through the public ``query.search`` seam with embeddings
supplied by the task #10 replay provider (the external embedding boundary);
no internal ``query`` function is monkeypatched.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import query
from query import SearchFilters
from shiori.config import load_config
from shiori.embedding_replay import composite_key, model_identity_fingerprint

MODEL_ID = "voyageai/voyage-4-nano"
MODEL_REVISION = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
DIM = 1024

# Fixed unit vector along the first axis; the replay fixture maps the query
# text and the old document to this.
QUERY_EMB = [1.0] + [0.0] * (DIM - 1)
# Unit vector 60 degrees off QUERY_EMB (cos = 0.5) — clearly weaker match.
FAR_EMB = [0.5] + [0.8660254] + [0.0] * (DIM - 2)

QUERY_TEXT = "zzqx no-bm25-match 4e2"
LATEST_QUERY_TEXT = "latest zzqx no-bm25-match 4e2"
NOT_LATEST_QUERY_TEXT = "not latest zzqx no-bm25-match 4e2"
ABSOLUTE_DATE_QUERY_TEXT = "2024-01-15 zzqx no-bm25-match 4e2"
NOT_THE_LATEST_QUERY_TEXT = "not the latest zzqx no-bm25-match 4e2"
LATEST_CJK_QUERY_TEXT = "最新预算状态 zzqx no-bm25-match 4e2"
LATEST_JA_QUERY_TEXT = "直近の予算状況 zzqx no-bm25-match 4e2"
RELATIVE_DAYS_QUERY_TEXT = "in the last 30 days zzqx no-bm25-match 4e2"
RELATIVE_DAYS_0_QUERY_TEXT = "in the last 0 days zzqx no-bm25-match 4e2"
RELATIVE_DAYS_366_QUERY_TEXT = "in the last 366 days zzqx no-bm25-match 4e2"
RELATIVE_DAYS_1000_QUERY_TEXT = "in the last 1000 days zzqx no-bm25-match 4e2"
RELATIVE_DAYS_FW30_QUERY_TEXT = "in the last ３０ days zzqx no-bm25-match 4e2"
RELATIVE_CN_QUERY_TEXT = "过去30天的预算状态 zzqx no-bm25-match 4e2"
RELATIVE_JA_QUERY_TEXT = "過去30日の予算状況 zzqx no-bm25-match 4e2"
NOT_LATEST_JA_QUERY_TEXT = "最新ではない予算状況 zzqx no-bm25-match 4e2"
NOT_RECENT_JA_QUERY_TEXT = "直近ではない予算状況 zzqx no-bm25-match 4e2"
OLD_CONTENT = "shiori_test_4e2_old_relevant_fact"
NEW_CONTENT = "shiori_test_4e2_new_weak_match"

# Every process-level query global that apply_settings may mutate.
_SETTINGS_GLOBALS = (
    "VOYAGE_API_URL",
    "VOYAGE_MODEL",
    "VOYAGE_KEY_PATH",
    "VOYAGE_API_KEY",
    "EMBEDDING_PROVIDER",
    "REPLAY_MANIFEST",
    "PG_CRED_PATH",
    "DATABASE_DSN",
    "EMBED_DIM",
)


@pytest.fixture
def replay_settings(tmp_path):
    """Activate the replay provider through the public config surface, and
    restore every query global apply_settings touched at test teardown."""
    manifest_path = _write_fixture(tmp_path)
    settings = load_config(
        environ={
            "SHIORI_EMBEDDING_PROVIDER": "replay",
            "SHIORI_REPLAY_MANIFEST": str(manifest_path),
            "SHIORI_EMBED_DIM": str(DIM),
            # conftest copies SHIORI_TEST_DATABASE_DSN into SHIORI_DATABASE_DSN.
            "SHIORI_DATABASE_DSN": os.environ["SHIORI_DATABASE_DSN"],
        }
    )
    settings.require_embedding()
    saved = {name: getattr(query, name) for name in _SETTINGS_GLOBALS}
    query.apply_settings(settings)
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(query, name, value)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vectors() -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for input_type, text, vec in (
        ("query", QUERY_TEXT, QUERY_EMB),
        ("query", LATEST_QUERY_TEXT, QUERY_EMB),
        ("query", NOT_LATEST_QUERY_TEXT, QUERY_EMB),
        ("query", ABSOLUTE_DATE_QUERY_TEXT, QUERY_EMB),
        ("query", NOT_THE_LATEST_QUERY_TEXT, QUERY_EMB),
        ("query", LATEST_CJK_QUERY_TEXT, QUERY_EMB),
        ("query", LATEST_JA_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_DAYS_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_DAYS_0_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_DAYS_366_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_DAYS_1000_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_DAYS_FW30_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_CN_QUERY_TEXT, QUERY_EMB),
        ("query", RELATIVE_JA_QUERY_TEXT, QUERY_EMB),
        ("query", NOT_LATEST_JA_QUERY_TEXT, QUERY_EMB),
        ("query", NOT_RECENT_JA_QUERY_TEXT, QUERY_EMB),
        ("document", OLD_CONTENT, QUERY_EMB),
        ("document", NEW_CONTENT, FAR_EMB),
    ):
        out[composite_key(MODEL_ID, MODEL_REVISION, input_type, text)] = vec
    return out


def _manifest(vectors: dict[str, list[float]]) -> dict:
    vec_sha = hashlib.sha256(
        json.dumps(vectors, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "shiori-replay-fixture/v1",
        "generator": {
            "libraries": {},
            "library": "sentence-transformers",
            "model": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "name": "4e2-slice1",
            "revision": "2026-08-12-1",
        },
        "model": {
            "dimension": DIM,
            "dtype": "float32",
            "id": MODEL_ID,
            "key_identity": model_identity_fingerprint(MODEL_ID, MODEL_REVISION),
            "normalized": True,
            "prompt_identity": {"document": "encode_document", "query": "encode_query"},
            "revision": MODEL_REVISION,
        },
        "corpus": {"count": 2, "input_type": "document", "sha256": "", "version": 1},
        "queries": {"count": 16, "input_type": "query", "sha256": "", "version": 1},
        "vectors": {
            "count": len(vectors),
            "key_format": "model_identity_fingerprint:input_type:sha256(text)",
            "sha256": vec_sha,
        },
    }


def _write_fixture(tmp_path: Path) -> Path:
    vectors = _vectors()
    corpus = "".join(f"{json.dumps({'content': t})}\n" for t in (OLD_CONTENT, NEW_CONTENT))
    queries = "".join(
        f"{json.dumps({'content': t})}\n"
        for t in (
            QUERY_TEXT,
            LATEST_QUERY_TEXT,
            NOT_LATEST_QUERY_TEXT,
            ABSOLUTE_DATE_QUERY_TEXT,
            NOT_THE_LATEST_QUERY_TEXT,
            LATEST_CJK_QUERY_TEXT,
            LATEST_JA_QUERY_TEXT,
            RELATIVE_DAYS_QUERY_TEXT,
            RELATIVE_DAYS_0_QUERY_TEXT,
            RELATIVE_DAYS_366_QUERY_TEXT,
            RELATIVE_DAYS_1000_QUERY_TEXT,
            RELATIVE_DAYS_FW30_QUERY_TEXT,
            RELATIVE_CN_QUERY_TEXT,
            RELATIVE_JA_QUERY_TEXT,
            NOT_LATEST_JA_QUERY_TEXT,
            NOT_RECENT_JA_QUERY_TEXT,
        )
    )
    (tmp_path / "corpus.jsonl").write_text(corpus, encoding="utf-8")
    (tmp_path / "queries.jsonl").write_text(queries, encoding="utf-8")
    (tmp_path / "vectors.json").write_text(
        json.dumps(vectors, sort_keys=True), encoding="utf-8"
    )
    manifest = _manifest(vectors)
    manifest["corpus"]["sha256"] = hashlib.sha256(corpus.encode("utf-8")).hexdigest()
    manifest["queries"]["sha256"] = hashlib.sha256(queries.encode("utf-8")).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _insert(conn, sid, content, emb, ts):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
        (sid, "main_user", content, str(emb), f"{MODEL_ID}@{MODEL_REVISION}",
         ts, ts, 0, 0, content, ts),
    )
    conn.commit()
    cur.close()


def test_ordinary_query_does_not_decay_old_relevant_fact(db, replay_settings):
    """An ordinary fact query ranks an older, clearly relevant chunk ABOVE a
    newer, weaker one: unconditional temporal decay must not apply.

    The embedding boundary is the task #10 replay provider, configured through
    the public settings surface; the query embedding is NOT an internal
    monkeypatch.  Rows carry the replay fixture's model identity, and the
    candidate set is bounded to this session via public SearchFilters.
    """
    conn, prefix = db
    sid = prefix + "-4e2slice1"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)


def test_search_page_ordering_and_pagination_consistent_with_decay_intent(db, replay_settings):
    """CHARACTERIZATION (not TDD red): ``search_page`` must reflect the same
    intent-gated ordering as ``search`` (ordinary: old-first; ``latest``:
    new-first) and pagination must be stable (limit/offset/has_more/next_offset
    truthful) for both."""
    conn, prefix = db
    sid = prefix + "-4e2page"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])

    ordinary = query.search_page(QUERY_TEXT, limit=2, filters=filters)
    assert [r[0] for r in ordinary.results] == [OLD_CONTENT, NEW_CONTENT]
    assert ordinary.limit == 2 and ordinary.offset == 0
    assert ordinary.has_more is False
    assert ordinary.next_offset is None

    latest = query.search_page(LATEST_QUERY_TEXT, limit=1, filters=filters)
    assert [r[0] for r in latest.results] == [NEW_CONTENT]
    assert latest.has_more is True
    assert latest.next_offset == 1

    latest_p2 = query.search_page(LATEST_QUERY_TEXT, limit=1, offset=1, filters=filters)
    assert [r[0] for r in latest_p2.results] == [OLD_CONTENT]
    assert latest_p2.has_more is False
    assert latest_p2.next_offset is None


def test_structured_time_bounds_still_apply_original_decay(db, replay_settings):
    """CHARACTERIZATION (not a new TDD red): under explicit structured time
    bounds that cover both records, the original 30-day decay still applies,
    so the newer weaker match ranks first.  This preserves the pre-4E2
    behavior for explicit temporal intent."""
    conn, prefix = db
    sid = prefix + "-4e2time"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    # Explicit time bounds covering both rows (old=now-120d, new=now): the
    # lower bound is before the oldest row, so neither record is filtered out.
    filters = SearchFilters.from_inputs(
        session_ids=[sid],
        time_from=old - timedelta(days=1),
    )
    res = query.search(QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    # Original decay formula preserved: new weak (cos=0.5, 1.0 decay) leads
    # old relevant (cos=1.0 * 2^-4 = 0.0625).
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_latest_token_marks_explicit_recency_intent(db, replay_settings):
    """A standalone English ``latest`` token is EXPLICIT recency intent: the
    original 30-day decay applies, so the newer weaker match leads.

    Genuine red for the first grammar slice: the current intermediate
    implementation only recognizes structured time bounds, so the ``latest``
    query is treated as ordinary and does NOT decay, leaving the old relevant
    fact (cos=1.0) first.  NFKC+casefold + token-boundary matching is the
    frozen grammar contract; this slice asserts only the observable order.
    """
    conn, prefix = db
    sid = prefix + "-4e2latest"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(LATEST_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_not_latest_vetoes_recency_intent(db, replay_settings):
    """An explicit ``not latest`` veto (standalone ``not`` + whitespace +
    standalone ``latest``) must conservatively disable text decay, so the older
    clearly-relevant fact leads.

    Genuine red for the veto slice: the current implementation only sees the
    standalone ``latest`` token and wrongly applies decay, leaving the new weak
    match first.  The frozen rule is NFKC+casefold then the exact token
    sequence ``not`` + whitespace + ``latest``.
    """
    conn, prefix = db
    sid = prefix + "-4e2notlatest"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(NOT_LATEST_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)


def test_absolute_date_alone_is_not_recency_intent(db, replay_settings):
    """CHARACTERIZATION (not a new TDD red): a bare absolute date is just asking
    about that date, not "prefer the newest", so no text decay applies and the
    older clearly-relevant fact leads.  The current implementation already
    behaves this way (no date grammar yet); first green is expected."""
    conn, prefix = db
    sid = prefix + "-4e2absdate"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(ABSOLUTE_DATE_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)


def test_not_the_latest_vetoes_recency_intent(db, replay_settings):
    """The explicit veto ``not the latest`` (standalone ``not`` + whitespace +
    optional standalone ``the`` + whitespace + standalone ``latest``) must
    disable text decay, so the older clearly-relevant fact leads.

    Genuine red: the current implementation only recognizes ``not latest``
    (no optional ``the``), so ``not the latest`` wrongly matches positive
    ``latest`` and decays.
    """
    conn, prefix = db
    sid = prefix + "-4e2notthelatest"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(NOT_THE_LATEST_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)


def test_latest_cjk_prefix_marks_explicit_recency_intent(db, replay_settings):
    """A leading CJK ``最新`` prefix (after NFKC, ignoring leading whitespace)
    is EXPLICIT "prefer the newest" intent: the original 30-day decay applies,
    so the newer weaker match leads.

    Genuine red: the current implementation only recognizes English
    ``latest``/structured bounds, so the ``最新`` query is treated as ordinary
    and does NOT decay, leaving the old relevant fact (cos=1.0) first.
    """
    conn, prefix = db
    sid = prefix + "-4e2latestcjk"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(LATEST_CJK_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_latest_ja_prefix_marks_explicit_recency_intent(db, replay_settings):
    """A leading Japanese ``直近`` prefix (after NFKC, ignoring leading
    whitespace) is EXPLICIT "prefer recent" intent: the original 30-day decay
    applies, so the newer weaker match leads.

    Genuine red: the current implementation only recognizes English
    ``latest``/structured bounds/``最新`` prefix, so ``直近`` is treated as
    ordinary and does NOT decay, leaving the old relevant fact first.
    """
    conn, prefix = db
    sid = prefix + "-4e2latestja"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(LATEST_JA_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_relative_last_days_marks_explicit_recency_intent(db, replay_settings):
    """A standalone ``last <1..365> day|days`` sequence is EXPLICIT relative
    recency intent: the original 30-day decay applies, so the newer weaker
    match leads.

    Genuine red: the current implementation only recognizes ``latest`` /
    ``最新`` / ``直近`` / structured bounds, so ``in the last 30 days`` is
    treated as ordinary and does NOT decay, leaving the old relevant fact
    first.
    """
    conn, prefix = db
    sid = prefix + "-4e2rel30d"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(RELATIVE_DAYS_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


@pytest.mark.parametrize(
    "query_text",
    [RELATIVE_DAYS_0_QUERY_TEXT, RELATIVE_DAYS_366_QUERY_TEXT, RELATIVE_DAYS_1000_QUERY_TEXT],
)
def test_relative_days_out_of_range_is_not_recency_intent(db, replay_settings, query_text):
    """CHARACTERIZATION (not TDD red): ``last N days`` with N outside 1..365 is
    not recency intent, so no decay applies and the older relevant fact leads.
    First green is expected."""
    conn, prefix = db
    sid = prefix + "-4e2relrange"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(query_text, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)


def test_relative_days_fullwidth_30_is_recency_intent(db, replay_settings):
    """CHARACTERIZATION (not TDD red): full-width ``３０`` is NFKC-folded to
    ASCII ``30``, so ``last ３０ days`` is recency intent (in range) and the
    newer weaker match leads.  First green is expected."""
    conn, prefix = db
    sid = prefix + "-4e2relfw"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(RELATIVE_DAYS_FW30_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_relative_cn_prefix_marks_explicit_recency_intent(db, replay_settings):
    """A leading Chinese ``过去`` + <1..365> + ``天`` prefix (after NFKC,
    ignoring leading whitespace) is EXPLICIT relative recency intent: the
    original 30-day decay applies, so the newer weaker match leads.

    Genuine red: the current implementation only recognizes English
    ``latest``/``last N days``/``最新``/``直近``/structured bounds, so
    ``过去30天`` is treated as ordinary and does NOT decay.
    """
    conn, prefix = db
    sid = prefix + "-4e2relcn"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(RELATIVE_CN_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


def test_relative_ja_prefix_marks_explicit_recency_intent(db, replay_settings):
    """A leading Japanese ``過去`` + <1..365> + ``日`` prefix (after NFKC,
    ignoring leading whitespace) is EXPLICIT relative recency intent: the
    original 30-day decay applies, so the newer weaker match leads.

    Genuine red: the current implementation only recognizes English
    ``latest``/``last N days``/``最新``/``直近``/``过去N天``/structured bounds,
    so ``過去30日`` is treated as ordinary and does NOT decay.
    """
    conn, prefix = db
    sid = prefix + "-4e2relja"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(RELATIVE_JA_QUERY_TEXT, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(NEW_CONTENT) < contents.index(OLD_CONTENT)


@pytest.mark.parametrize(
    ("query_text", "prefix"),
    [
        (NOT_LATEST_JA_QUERY_TEXT, "最新ではない"),
        (NOT_RECENT_JA_QUERY_TEXT, "直近ではない"),
    ],
)
def test_ja_negated_prefix_vetoes_recency_intent(db, replay_settings, query_text, prefix):
    """A Japanese negated-prefix operator (``最新ではない`` / ``直近ではない``
    at the start of the lstrip'd folded text) conservatively vetoes text decay,
    so the older clearly-relevant fact leads.

    Genuine red: the current implementation recognizes the positive
    ``最新``/``直近`` prefixes and wrongly decays.
    """
    conn, prefix_ = db
    sid = prefix_ + "-4e2janeg"
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)

    filters = SearchFilters.from_inputs(session_ids=[sid])
    res = query.search(query_text, limit=300, filters=filters)
    mine = [r for r in res if r[3] == sid]
    assert len(mine) == 2, f"expected exactly our 2 chunks, got {len(mine)}"
    contents = [r[0] for r in mine]
    assert contents.index(OLD_CONTENT) < contents.index(NEW_CONTENT)
