import ingest_discord

from helpers import count_chunks, make_discord_chunk


def test_discord_missing_embedding_preserves_existing(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "alpha one"), make_discord_chunk(1, sid, "alpha two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "beta one"), make_discord_chunk(3, sid, "beta two")],
        [emb, None], [1], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_discord_failed_indices_only_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "x one"), make_discord_chunk(1, sid, "x two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "y one"), make_discord_chunk(3, sid, "y two")],
        [emb, emb], [1], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_discord_out_of_bounds_failed_index_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "oob one"), make_discord_chunk(1, sid, "oob two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "z one"), make_discord_chunk(3, sid, "z two")],
        [emb, emb], [999], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_discord_length_mismatch_aborts(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "m one"), make_discord_chunk(1, sid, "m two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "n one"), make_discord_chunk(3, sid, "n two"),
         make_discord_chunk(4, sid, "n three")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (0, 0)
    assert count_chunks(conn, sid) == before


def test_discord_success_full_replace(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "pass one"), make_discord_chunk(1, sid, "pass two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "reb one"), make_discord_chunk(3, sid, "reb two"),
         make_discord_chunk(4, sid, "reb three")],
        [emb, emb, emb], [], conn,
    )
    assert (stored, failed) == (3, 0)
    assert count_chunks(conn, sid) == 3


def test_discord_insert_failure_rolls_back(db, emb, wrong_emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "gamma one"), make_discord_chunk(1, sid, "gamma two")],
        [emb, emb], [], conn,
    )
    assert (stored, failed) == (2, 0)
    before = count_chunks(conn, sid)

    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(2, sid, "delta one"), make_discord_chunk(3, sid, "delta two")],
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


def test_discord_channel_field_stored(db, emb):
    conn, sid = db
    stored, failed = ingest_discord.store_chunks(
        [make_discord_chunk(0, sid, "chan one", channel="general")],
        [emb], [], conn,
    )
    assert (stored, failed) == (1, 0)
    cur = conn.cursor()
    cur.execute(
        "SELECT channel, source_type FROM session_chunks WHERE session_id = %s",
        (sid,),
    )
    channel, source_type = cur.fetchone()
    cur.close()
    assert channel == "general"
    assert source_type == "discord"
