from datetime import datetime, timedelta, timezone

import pytest

import ingest
import query

from conftest import VALID_EMB
from helpers import make_chunk


def _ts(chunk, conn, sid, content):
    cur = conn.cursor()
    cur.execute(
        "SELECT timestamp_start, timestamp_end FROM session_chunks "
        "WHERE session_id = %s AND content = %s",
        (sid, content),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _null_ts_chunk(i, sid, content):
    c = make_chunk(i, sid, content)
    c["timestamp_start"] = None
    c["timestamp_end"] = None
    return c


# ── Store-level: file mtime written when ts parse fails ─────────────────────
def test_null_ts_chunk_gets_file_mtime_fallback(db, emb):
    conn, sid = db
    old = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    stored, failed = ingest.store_chunks(
        [_null_ts_chunk(0, sid, "nts one")], [emb], [], conn, fallback_ts=old,
    )
    assert (stored, failed) == (1, 0)
    ts_start, ts_end = _ts(None, conn, sid, "nts one")
    assert ts_start == old, "unparseable ts must fall back to the file mtime"
    assert ts_end == old


def test_reingest_null_ts_does_not_become_new(db, emb):
    conn, sid = db
    old = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    ingest.store_chunks([_null_ts_chunk(0, sid, "rst one")], [emb], [], conn, fallback_ts=old)
    ts_start, _ = _ts(None, conn, sid, "rst one")
    assert ts_start == old

    # Re-ingest of the same file (same mtime) must keep the SAME ts, not reset
    # to now() — otherwise old NULL-ts memory would be re-ranked as new.
    ingest.store_chunks([_null_ts_chunk(0, sid, "rst one")], [emb], [], conn, fallback_ts=old)
    ts_start2, _ = _ts(None, conn, sid, "rst one")
    assert ts_start2 == old, "re-ingest must not bump NULL-ts memory to now()"


# ── Query-level: discrimination via stored mtime, not INSERT created_at ─────
def _insert_fallback(conn, sid, content, emb, fallback):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
        (sid, "main_user", content, str(emb), "voyage-4-large",
         fallback, fallback, 0, 0, content, fallback),
    )
    conn.commit()
    cur.close()


# Fixed unit vectors from test_query.py
QUERY_EMB = [1.0] + [0.0] * 1023
FAR_EMB = [0.5] + [0.8660254] + [0.0] * 1022


def test_null_ts_decay_discriminates_by_stored_mtime(db, monkeypatch):
    conn, prefix = db
    monkeypatch.setattr(query, "embed_query", lambda q: QUERY_EMB)
    now = datetime.now(timezone.utc)

    # Two NULL-ts-equivalent chunks (no parseable message time) stored with
    # different file mtimes. Decay must be driven by the stored mtime, so the
    # older file's chunk is ranked below the newer file's chunk.
    recent_sid = prefix + "-recent"
    old_sid = prefix + "-old"
    _insert_fallback(conn, recent_sid, "shiyi_nts_recent_x", FAR_EMB, now)
    _insert_fallback(conn, old_sid, "shiyi_nts_old_x", QUERY_EMB, now - timedelta(days=120))

    res = query.search("zzqx no-bm25-match 9", limit=300)
    mine = [r for r in res if r[3] in (recent_sid, old_sid)]
    assert len(mine) == 2
    contents = [r[0] for r in mine]
    assert contents.index("shiyi_nts_recent_x") < contents.index("shiyi_nts_old_x")
