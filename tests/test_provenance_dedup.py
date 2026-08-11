"""Provenance-aware dedup (task #33): distinct content must survive MMR.

The public seam is the real isolated PostgreSQL behind ``query.search``.  The
replay provider is only a system-boundary configuration that supplies the fixed
query embedding; no internal ``query`` collaborator is mocked and no model or
network runs at test time (vectors are committed static fixtures).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import query
from shiori.embedding_replay import ReplayEmbedder

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "replay_provenance_dedup"
MANIFEST = FIXTURES / "manifest.json"
MODEL_IDENTITY = "voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f"

# Frozen pair (A/B): same session, same source, byte-different content, same
# time semantics.  cosine(A, B) = 0.9452 > MMR_SIM_THRESHOLD (0.85).
A = "[user] The deadline for the quarterly report was moved to the end of August."
B = "[user] The quarterly report deadline is now the last working day of August."
QUERY = "when is the quarterly report deadline?"


def _insert(conn, session_id, content, embedding, ts, source_type="synthetic-note"):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
        (session_id, source_type, content, str(embedding), MODEL_IDENTITY,
         ts, ts, 0, 0, content, ts),
    )
    conn.commit()
    cur.close()


@pytest.fixture
def replay_query(monkeypatch):
    """Configure the public replay provider boundary (fixed query embedding)."""
    embedder = ReplayEmbedder.from_files(MANIFEST, MANIFEST.with_name("vectors.json"))
    monkeypatch.setattr(query, "EMBEDDING_PROVIDER", "replay")
    monkeypatch.setattr(query, "REPLAY_MANIFEST", str(MANIFEST))
    monkeypatch.setattr(query, "EMBED_DIM", 1024)
    monkeypatch.setattr(query, "VOYAGE_MODEL", MODEL_IDENTITY)
    return embedder


def test_dedup_keeps_distinct_content_in_same_session(replay_query, db):
    conn, prefix = db
    session_id = prefix + "-bench-plan"
    now = datetime.now(UTC)
    # DB INSERT is isolated fixture setup only; every assertion observes
    # query.search's public return value.
    _insert(conn, session_id, B, replay_query.embed(B, input_type="document"), now)
    _insert(conn, session_id, A, replay_query.embed(A, input_type="document"), now)

    results = query.search(QUERY, limit=20)
    mine = [(row[0], row[3]) for row in results if row[3] == session_id]

    # Human-spec literal order: current fact (B) before historical fact (A).
    # Both must be present even though cosine(A, B) > 0.85.
    assert mine == [(B, session_id), (A, session_id)]


def test_dedup_collapses_exact_duplicate_in_same_provenance(replay_query, db):
    conn, prefix = db
    session_id = prefix + "-exact-dup"
    now = datetime.now(UTC)
    # Two byte-identical A rows: same session_id, source_type, embedding, and
    # time semantics.  True duplication must still collapse to one.
    _insert(conn, session_id, A, replay_query.embed(A, input_type="document"), now)
    _insert(conn, session_id, A, replay_query.embed(A, input_type="document"), now)

    results = query.search(QUERY, limit=20)
    mine = [(row[0], row[3]) for row in results if row[3] == session_id]

    assert mine == [(A, session_id)]


def test_dedup_keeps_identical_content_across_sessions(replay_query, db):
    conn, prefix = db
    session_id_a = prefix + "-sess-a"
    session_id_b = prefix + "-sess-b"
    now = datetime.now(UTC)
    # Byte-identical A in two distinct sessions: identical embedding,
    # source_type, and time semantics, but different session_id provenance.
    # Both must survive dedup because provenance differs.
    _insert(conn, session_id_a, A, replay_query.embed(A, input_type="document"), now)
    _insert(conn, session_id_b, A, replay_query.embed(A, input_type="document"), now)

    results = query.search(QUERY, limit=20)
    mine = sorted((row[0], row[3]) for row in results if row[3] in (session_id_a, session_id_b))

    assert mine == sorted([(A, session_id_a), (A, session_id_b)])
