
from helpers import count_chunks, make_chunk

import ingest


def test_missing_embedding_preserves_existing(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "alpha one"), make_chunk(1, sid, "alpha two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    # One chunk's embedding missing → whole batch aborted, nothing written.
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "beta one"), make_chunk(3, sid, "beta two")],
        [emb, None], [1], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_empty_chunks_is_noop(db):
    conn, sid = db
    stored, failed = ingest.store_chunks([], [], [], conn)
    assert (stored, failed) == (0, 0)


def test_failed_indices_only_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "x one"), make_chunk(1, sid, "x two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    # failed_indices set but embeddings present → still aborts (data preserved).
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "y one"), make_chunk(3, sid, "y two")],
        [emb, emb], [1], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_out_of_bounds_failed_index_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "oob one"), make_chunk(1, sid, "oob two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    # failed_indices points past len(chunks); embeddings are all present and
    # length matches. A range bug lets this slip past the guard and rebuild.
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "z one"), make_chunk(3, sid, "z two")],
        [emb, emb], [999], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_length_mismatch_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "m one"), make_chunk(1, sid, "m two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    # 3 chunks but only 2 embeddings → length mismatch → abort, preserve.
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "n one"), make_chunk(3, sid, "n two"), make_chunk(4, sid, "n three")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_success_full_replace(db, emb):
    conn, sid = db
    # First pass: 2 chunks.
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "pass one"), make_chunk(1, sid, "pass two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)

    # Second pass: 3 chunks for the same session → old 2 deleted, 3 inserted.
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "reb one"), make_chunk(3, sid, "reb two"), make_chunk(4, sid, "reb three")],
        [emb, emb, emb], [], conn,
    )
    assert (stored, failed) == (3, 0)
    assert count_chunks(conn, sid) == 3


def test_success_incremental_append_preserves_existing(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(1, sid, "old turn")], [emb], [], conn,
    )
    assert (stored, failed) == (1, 0)

    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "new turn")], [emb], [], conn, replace=False,
    )

    assert (stored, failed) == (1, 0)
    assert count_chunks(conn, sid) == 2
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM session_chunks WHERE session_id = %s ORDER BY turn_index_start",
        (sid,),
    )
    assert [row[0] for row in cur.fetchall()] == ["old turn", "new turn"]
    cur.close()


def test_insert_failure_rolls_back_whole_batch(db, emb, wrong_emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "gamma one"), make_chunk(1, sid, "gamma two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    # Second chunk has a wrong-dimension embedding → INSERT raises → whole batch
    # (including the DELETE + first INSERT) must be rolled back.
    stored, failed = ingest.store_chunks(
        [make_chunk(2, sid, "delta one"), make_chunk(3, sid, "delta two")],
        [emb, wrong_emb], [], conn,
    )
    assert (stored, failed) == (0, 1)
    assert count_chunks(conn, sid) == before

    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM session_chunks WHERE session_id = %s AND content LIKE 'delta %%'",
        (sid,),
    )
    assert cur.fetchone()[0] == 0
    cur.execute(
        "SELECT count(*) FROM session_chunks WHERE session_id = %s AND content LIKE 'gamma %%'",
        (sid,),
    )
    assert cur.fetchone()[0] == 2
    cur.close()


def test_bad_timestamp_stores_null(db, emb):
    conn, sid = db
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "garbage ts", ts_start="not-a-ts", ts_end="also-bad")],
        [emb], [], conn,
    )
    assert (stored, failed) == (1, 0)
    cur = conn.cursor()
    cur.execute(
        "SELECT timestamp_start, timestamp_end FROM session_chunks WHERE session_id = %s AND content = 'garbage ts'",
        (sid,),
    )
    ts_start, ts_end = cur.fetchone()
    cur.close()
    assert ts_start is None
    assert ts_end is None
