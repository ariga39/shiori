def make_chunk(i, sid, content=None, ts_start="2026-08-03T00:00:00Z",
               ts_end="2026-08-03T00:00:01Z", source_type="main_user"):
    return {
        "session_id": sid,
        "source_type": source_type,
        "content": content if content is not None else f"test chunk {i}",
        "timestamp_start": ts_start,
        "timestamp_end": ts_end,
        "turn_index_start": i,
        "turn_index_end": i,
    }


def make_discord_chunk(i, sid, content=None, channel="general"):
    c = make_chunk(i, sid, content)
    c["source_type"] = "discord"
    c["channel"] = channel
    return c


def count_chunks(conn, session_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM session_chunks WHERE session_id = %s",
        (session_id,),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n
