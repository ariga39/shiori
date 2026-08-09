import tiktoken

import ingest
import ingest_discord

_enc = tiktoken.get_encoding("cl100k_base")


def _msg(line_no, role, content, ts="2026-08-03T00:00:00Z"):
    return {
        "type": "message",
        "timestamp": ts,
        "_line_no": line_no,
        "message": {"role": role, "content": content},
    }


def test_short_single_message_yields_one_chunk():
    msgs = [_msg(1, "user", "hello world this is a short message")]
    chunks = ingest.chunk_messages(msgs, "sess-1", "main_user")
    assert len(chunks) == 1
    c = chunks[0]
    assert c["session_id"] == "sess-1"
    assert c["source_type"] == "main_user"
    assert "[user]" in c["content"]
    assert c["turn_index_start"] == 1
    assert c["turn_index_end"] == 1


def test_long_text_produces_multiple_chunks_within_token_window():
    msgs = [_msg(1, "user", "alpha bravo charlie " * 200)]
    chunks = ingest.chunk_messages(msgs, "sess-1", "main_user")
    assert len(chunks) > 1
    for c in chunks:
        toks = _enc.encode(c["content"])
        assert len(toks) <= ingest.CHUNK_TOKENS


def test_consecutive_chunks_overlap():
    msgs = [_msg(1, "user", "delta echo foxtrot " * 300)]
    chunks = ingest.chunk_messages(msgs, "sess-1", "main_user")
    assert len(chunks) >= 2
    a_toks = _enc.encode(chunks[0]["content"])
    b_toks = _enc.encode(chunks[1]["content"])
    overlap = len(set(a_toks) & set(b_toks))
    assert overlap > 0


def test_chunk_timestamps_map_first_and_last_overlapping_message():
    msgs = [
        _msg(1, "user", "first message alpha " * 200, ts="2026-08-03T01:00:00Z"),
        _msg(2, "assistant", "second message beta " * 200, ts="2026-08-03T02:00:00Z"),
    ]
    chunks = ingest.chunk_messages(msgs, "sess-1", "main_user")
    assert len(chunks) > 1
    first = chunks[0]
    assert first["timestamp_start"] == "2026-08-03T01:00:00Z"
    last = chunks[-1]
    assert last["timestamp_end"] == "2026-08-03T02:00:00Z"
    assert last["turn_index_end"] == 2


def test_no_qualifying_messages_returns_empty():
    chunks = ingest.chunk_messages([], "sess-1", "main_user")
    assert chunks == []


def test_tool_messages_filtered_out_before_chunking():
    msgs = [
        {"type": "message", "_line_no": 1, "message": {"role": "tool", "content": "x" * 50}},
        _msg(2, "user", "only user text here"),
    ]
    chunks = ingest.chunk_messages(msgs, "sess-1", "main_user")
    assert len(chunks) == 1
    assert "tool" not in chunks[0]["content"]
    assert "[user]" in chunks[0]["content"]


# ── ingest_discord.build_chunks ──────────────────────────────────────────────


def _dmsg(i, content, ts="2026-08-03T00:00:00+00:00", msg_id="id0"):
    return {
        "id": msg_id or f"id{i}",
        "type": 0,
        "timestamp": ts,
        "author": {"global_name": "alice", "username": "alice"},
        "content": content,
        "attachments": [],
        "embeds": [],
    }


def test_discord_short_channel_yields_one_chunk():
    msgs = [_dmsg(0, "short discord hello")]
    chunks = ingest_discord.build_chunks(msgs, "general")
    assert len(chunks) == 1
    c = chunks[0]
    assert c["session_id"] == "discord-general"
    assert c["source_type"] == "discord"
    assert c["channel"] == "general"
    assert "alice:" in c["content"]


def test_discord_long_channel_multiple_chunks():
    msgs = [_dmsg(0, "golf hotel india " * 300)]
    chunks = ingest_discord.build_chunks(msgs, "general")
    assert len(chunks) > 1
    for c in chunks:
        assert len(_enc.encode(c["content"])) <= ingest_discord.CHUNK_TOKENS


def test_discord_reply_type_included():
    msgs = [_dmsg(0, "a normal message here", msg_id="n1"), {"id": "r1", "type": 19, "timestamp": "2026-08-03T00:00:00+00:00", "author": {"username": "bob"}, "content": "a reply message", "attachments": [], "embeds": []}]
    chunks = ingest_discord.build_chunks(msgs, "general")
    assert len(chunks) == 1
    assert "bob:" in chunks[0]["content"]
    assert "alice:" in chunks[0]["content"]


def test_discord_channel_session_id_and_channel_mapping():
    chunks = ingest_discord.build_chunks([_dmsg(0, "just a test message")], "off-topic")
    assert chunks[0]["session_id"] == "discord-off-topic"
    assert chunks[0]["channel"] == "off-topic"
