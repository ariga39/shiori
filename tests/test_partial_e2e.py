import os
import uuid

import psycopg2
import pytest

import ingest
import ingest_discord

from conftest import VALID_EMB
from helpers import count_chunks, make_chunk, make_discord_chunk


def _open():
    creds = ingest.load_credentials()
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


def _cleanup(conn, file_path, sid):
    cur = conn.cursor()
    cur.execute("DELETE FROM ingestion_state WHERE file_path = %s", (file_path,))
    cur.execute("DELETE FROM session_chunks WHERE session_id = %s", (sid,))
    conn.commit()
    cur.close()


def _state_row(conn, file_path):
    cur = conn.cursor()
    cur.execute(
        "SELECT file_size, processed_offset, chunks_created FROM ingestion_state WHERE file_path = %s",
        (file_path,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _preset_old_chunks(conn, sid):
    # Seed existing memory for the session via the REAL store_chunks.
    stored, failed = ingest.store_chunks(
        [make_chunk(0, sid, "old one"), make_chunk(1, sid, "old two")],
        [VALID_EMB, VALID_EMB], [], conn,
    )
    assert (stored, failed) == (2, 0)


def test_ingest_embed_partial_failure_marks_partial_and_zero_size(tmp_path, monkeypatch):
    f = tmp_path / "sess.jsonl"
    f.write_text("x" * 100)
    path = str(f)
    sid = ingest.derive_session_id(path)

    monkeypatch.setattr(ingest, "get_db", _open)
    monkeypatch.setattr(ingest, "find_session_files", lambda: [path])
    monkeypatch.setattr(ingest, "get_processed_files", lambda c: {})
    monkeypatch.setattr(ingest, "parse_session_file", lambda p: [{"type": "message"}])
    monkeypatch.setattr(ingest, "classify_session", lambda fp, lines: "main_user")
    monkeypatch.setattr(
        ingest, "chunk_messages",
        lambda m, s, st: [make_chunk(0, s, "hello world")],
    )
    # Embedding fails for the single chunk. embed is mocked; store_chunks is
    # REAL and must hit the atomic guard: no DELETE, no INSERT, count unchanged.
    monkeypatch.setattr(ingest, "embed_texts_with_retry", lambda texts: ([None], [0]))
    monkeypatch.setattr("sys.argv", ["ingest.py"])

    # Seed old chunks for this session before the failing run.
    pconn = _open()
    try:
        _preset_old_chunks(pconn, sid)
        pconn.close()
        before = None
        vconn = _open()
        try:
            before = count_chunks(vconn, sid)
        finally:
            vconn.close()
    finally:
        pass

    try:
        ingest.main()
    finally:
        vconn = _open()
        try:
            row = _state_row(vconn, path)
            after = count_chunks(vconn, sid)
        finally:
            _cleanup(vconn, path, sid)
            vconn.close()

    assert row is not None, "partial run must still checkpoint the file"
    file_size, offset, chunks = row
    assert file_size == 0, "partial failure must record file_size 0 to force retry"
    assert offset == os.path.getsize(path)
    assert chunks == 0
    assert before == after == 2, "atomic guard must preserve existing chunks on embed failure"


def test_discord_embed_partial_failure_marks_partial_and_zero_size(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    f = archive / "general.jsonl"
    f.write_text("x" * 100)
    path = str(f.resolve())
    sid = "discord-general"

    monkeypatch.setattr(ingest_discord, "get_db", _open)
    monkeypatch.setattr(ingest_discord, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(ingest_discord, "get_processed_files", lambda c: {})
    monkeypatch.setattr(ingest_discord, "load_messages", lambda p: [{"type": 0, "content": "hi"}])
    monkeypatch.setattr(
        ingest_discord, "build_chunks",
        lambda m, ch: [make_discord_chunk(0, "discord-general", "hello world")],
    )
    monkeypatch.setattr(ingest_discord, "embed_texts_with_retry", lambda texts: ([None], [0]))
    monkeypatch.setattr("sys.argv", ["ingest_discord.py"])

    # Seed old chunks via the REAL store_chunks before the failing run.
    pconn = _open()
    try:
        ingest_discord.store_chunks(
            [make_discord_chunk(0, sid, "old one"), make_discord_chunk(1, sid, "old two")],
            [VALID_EMB, VALID_EMB], [], pconn,
        )
        pconn.close()
    finally:
        pass

    try:
        ingest_discord.main()
    finally:
        vconn = _open()
        try:
            row = _state_row(vconn, path)
            after = count_chunks(vconn, sid)
        finally:
            _cleanup(vconn, path, sid)
            vconn.close()

    assert row is not None, "partial run must still checkpoint the file"
    file_size, offset, chunks = row
    assert file_size == 0, "partial failure must record file_size 0 to force retry"
    assert offset == os.path.getsize(f)
    assert chunks == 0
    assert after == 2, "atomic guard must preserve existing chunks on embed failure"
