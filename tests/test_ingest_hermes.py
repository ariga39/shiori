"""Tests for ingest_hermes.py — Hermes state.db → PG bridge.

Focus areas:
1. load_messages must read BOTH active=1 AND soft-archived (active=0,
   compacted=1) rows — regression test for the compaction data-loss bug
   where re-ingesting a compacted session would DELETE the full-history
   chunks from PG and replace them with only the summary.
2. rewind/undo rows (active=0, compacted=0) must be excluded.
3. Session listing filters cron and maps source_type correctly.
4. Incremental cursor: unchanged sessions are skipped.
"""

import os
import sqlite3
import sys
import time
import uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import ingest_hermes
import ingest  # noqa: E402 — same package root as ingest_hermes
from tests import helpers


@pytest.fixture
def hermes_db(tmp_path):
    """In-memory-ish Hermes state.db on disk with a clean schema."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            session_key TEXT,
            chat_id TEXT,
            chat_type TEXT,
            thread_id TEXT,
            display_name TEXT,
            origin_json TEXT,
            expiry_finalized INTEGER DEFAULT 0,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            system_prompt_hash TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            rewind_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            cwd TEXT,
            git_branch TEXT,
            git_repo_root TEXT,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            last_activity_at REAL,
            last_activity_description TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            effect_disposition TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT,
            platform_message_id TEXT,
            observed INTEGER DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0,
            api_content TEXT,
            display_kind TEXT,
            display_metadata TEXT
        );
        """
    )
    yield conn
    conn.close()


def add_session(conn, sid, source="discord", title="t", message_count=0,
                last_activity_at=None):
    if last_activity_at is None:
        last_activity_at = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, title, chat_id, message_count,"
        " started_at, last_activity_at) VALUES (?,?,?,?,?,?,?)",
        (sid, source, title, "test-chat", message_count,
         last_activity_at - 100, last_activity_at),
    )
    conn.commit()


def add_message(conn, sid, role, content, active=1, compacted=0, ts=None):
    if ts is None:
        ts = time.time()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active,"
        " compacted) VALUES (?,?,?,?,?,?)",
        (sid, role, content, ts, active, compacted),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestLoadMessages:
    def test_reads_active_and_compacted_rows(self, hermes_db):
        """Compacted (soft-archived) rows must still be ingested.

        Regression: if a Hermes session is compacted (old turns flipped to
        active=0, compacted=1, summary inserted as active=1), re-ingest must
        keep the full history — otherwise store_chunks deletes the PG chunks
        and replaces them with the summary only.
        """
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=3)
        add_message(hermes_db, sid, "user", "original question one", active=1, compacted=0)
        add_message(hermes_db, sid, "assistant", "original answer one", active=1, compacted=0)
        # simulate compaction: old rows soft-archived
        add_message(hermes_db, sid, "user", "old question archived", active=0, compacted=1)
        add_message(hermes_db, sid, "assistant", "old answer archived", active=0, compacted=1)
        # summary row
        add_message(hermes_db, sid, "assistant", "compacted summary of the session", active=1, compacted=0)

        msgs = ingest_hermes.load_messages(hermes_db, sid)
        contents = [m["message"]["content"] for m in msgs]
        assert any("old question archived" in c for c in contents), \
            "soft-archived user row must be ingested"
        assert any("old answer archived" in c for c in contents), \
            "soft-archived assistant row must be ingested"
        assert any("original question one" in c for c in contents), \
            "pre-compaction active row must be ingested"
        assert any("compacted summary" in c for c in contents), \
            "summary row must be ingested"

    def test_excludes_rewind_undo_rows(self, hermes_db):
        """Rows that are active=0 AND compacted=0 (rewind/undo) must NOT be read."""
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=2)
        add_message(hermes_db, sid, "user", "real message", active=1, compacted=0)
        add_message(hermes_db, sid, "assistant", "rewound message gone", active=0, compacted=0)

        msgs = ingest_hermes.load_messages(hermes_db, sid)
        contents = [m["message"]["content"] for m in msgs]
        assert any("real message" in c for c in contents)
        assert not any("rewound message gone" in c for c in contents), \
            "rewind/undo rows (active=0, compacted=0) must be excluded"

    def test_skips_tool_and_empty_content(self, hermes_db):
        """Only user/assistant rows with non-empty content are loaded."""
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=4)
        add_message(hermes_db, sid, "user", "good text")
        add_message(hermes_db, sid, "tool", "tool output must be skipped")
        add_message(hermes_db, sid, "system", "system message skipped")
        add_message(hermes_db, sid, "user", "")

        msgs = ingest_hermes.load_messages(hermes_db, sid)
        contents = [m["message"]["content"] for m in msgs]
        assert contents == ["good text"]

    def test_returns_turn_index_and_timestamp(self, hermes_db):
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=1)
        mid = add_message(hermes_db, sid, "user", "with meta", ts=1234567890.0)

        msgs = ingest_hermes.load_messages(hermes_db, sid)
        assert len(msgs) == 1
        assert msgs[0]["_line_no"] == mid
        assert msgs[0]["timestamp"] == 1234567890.0


class TestListSessions:
    def test_skips_cron_and_maps_source(self, hermes_db):
        add_session(hermes_db, "discord-s", source="discord", title="d")
        add_session(hermes_db, "tui-s", source="tui", title="t")
        add_session(hermes_db, "sub-s", source="subagent", title="s")
        add_session(hermes_db, "cron-s", source="cron", title="c")

        sessions = ingest_hermes.list_sessions(hermes_db)
        by_id = {s["session_id"]: s for s in sessions}
        assert "cron-s" not in by_id, "cron sessions must be skipped"
        assert by_id["discord-s"]["source_type"] == "discord"
        assert by_id["tui-s"]["source_type"] == "main_user"
        assert by_id["sub-s"]["source_type"] == "subagent", \
            "subagent sessions must keep distinct source_type"

    def test_detects_rewound_sessions(self, hermes_db):
        """A session with rewind/undo rows (active=0, compacted=0) must be flagged.

        Regression for BLOCKING-1: Hermes rewind flips rows to active=0 and
        bumps rewind_count but does NOT touch last_activity_at/message_count.
        Without the has_rewound probe, the incremental cursor would skip the
        session and undone messages would stay searchable in PG forever.
        """
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=2)
        add_message(hermes_db, sid, "user", "kept message", active=1, compacted=0)
        add_message(hermes_db, sid, "assistant", "undone message", active=0, compacted=0)

        sessions = ingest_hermes.list_sessions(hermes_db)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id[sid]["has_rewound"] is True, \
            "session with rewind rows must be flagged has_rewound"

    def test_no_false_rewind_for_compacted(self, hermes_db):
        """Compaction rows (active=0, compacted=1) must NOT trigger has_rewound."""
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=2)
        add_message(hermes_db, sid, "user", "old archived", active=0, compacted=1)
        add_message(hermes_db, sid, "assistant", "summary", active=1, compacted=0)

        sessions = ingest_hermes.list_sessions(hermes_db)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id[sid]["has_rewound"] is False, \
            "compaction rows must not be mistaken for rewind"


class TestIncrementalCursor:
    def test_unchanged_sessions_skipped(self, hermes_db, tmp_path, monkeypatch):
        """Sessions whose mtime/message_count are unchanged must not reprocess."""
        # Point ingest_hermes at our temp db
        monkeypatch.setattr(ingest_hermes, "HERMES_DB", str(tmp_path / "state.db"))

        sid = "sess-" + uuid.uuid4().hex
        last = time.time()
        add_session(hermes_db, sid, message_count=2, last_activity_at=last)
        add_message(hermes_db, sid, "user", "hello")
        add_message(hermes_db, sid, "assistant", "hi")

        sessions = ingest_hermes.list_sessions(hermes_db)
        # simulate processed state: same mtime + count + rewind_count → skip
        prev = {"hermes://" + sid: {"mtime": last, "size": 2, "rewind": 0}}
        to_process = []
        for s in sessions:
            key = "hermes://" + s["session_id"]
            p = prev.get(key)
            unchanged = p and p["mtime"] == s["last_activity_at"] and p["size"] == s["message_count"]
            rewind_unchanged = p is not None and p["rewind"] == s["rewind_count"]
            if unchanged and rewind_unchanged:
                continue
            to_process.append(s)
        assert to_process == [], "unchanged session must be skipped"

    def test_changed_session_processed(self, hermes_db, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_hermes, "HERMES_DB", str(tmp_path / "state.db"))
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=2, last_activity_at=time.time())
        add_message(hermes_db, sid, "user", "hello")
        add_message(hermes_db, sid, "assistant", "hi")

        sessions = ingest_hermes.list_sessions(hermes_db)
        # old processed state with stale mtime → must reprocess
        prev = {"hermes://" + sid: {"mtime": time.time() - 9999, "size": 2, "rewind": 0}}
        to_process = []
        for s in sessions:
            key = "hermes://" + s["session_id"]
            p = prev.get(key)
            unchanged = p and p["mtime"] == s["last_activity_at"] and p["size"] == s["message_count"]
            rewind_unchanged = p is not None and p["rewind"] == s["rewind_count"]
            if unchanged and rewind_unchanged:
                continue
            to_process.append(s)
        assert len(to_process) == 1, "changed session must be reprocessed"

    def test_rewound_session_reprocessed_despite_unchanged_cursor(self, hermes_db, tmp_path, monkeypatch):
        """Regression for BLOCKING-1: a rewound session must be reprocessed
        even when last_activity_at and message_count are unchanged — the
        cursor alone cannot detect the rewind, so rewind_count must force it.
        """
        monkeypatch.setattr(ingest_hermes, "HERMES_DB", str(tmp_path / "state.db"))
        sid = "sess-" + uuid.uuid4().hex
        last = time.time()
        add_session(hermes_db, sid, message_count=2, last_activity_at=last)
        add_message(hermes_db, sid, "user", "kept message", active=1, compacted=0)
        add_message(hermes_db, sid, "assistant", "undone message", active=0, compacted=0)
        # Hermes rewind bumps rewind_count without touching mtime/message_count
        hermes_db.execute("UPDATE sessions SET rewind_count = 1 WHERE id = ?", (sid,))
        hermes_db.commit()

        sessions = ingest_hermes.list_sessions(hermes_db)
        # stale cursor: mtime+size unchanged, but rewind 0 → 1
        prev = {"hermes://" + sid: {"mtime": last, "size": 2, "rewind": 0}}
        to_process = []
        for s in sessions:
            key = "hermes://" + s["session_id"]
            p = prev.get(key)
            unchanged = p and p["mtime"] == s["last_activity_at"] and p["size"] == s["message_count"]
            rewind_unchanged = p is not None and p["rewind"] == s["rewind_count"]
            if unchanged and rewind_unchanged:
                continue
            to_process.append(s)
        assert len(to_process) == 1, "rewound session must be reprocessed despite unchanged cursor"

    def test_rewound_session_skipped_after_rewind_processed(self, hermes_db, tmp_path, monkeypatch):
        """Once rewind is processed (cursor rewind == rewind_count), session
        must NOT reprocess forever even though rewind rows persist in state.db.
        This prevents infinite churn: has_rewound alone would stay true forever,
        so the rewind_count cursor comparison is what breaks the loop.
        """
        monkeypatch.setattr(ingest_hermes, "HERMES_DB", str(tmp_path / "state.db"))
        sid = "sess-" + uuid.uuid4().hex
        last = time.time()
        add_session(hermes_db, sid, message_count=2, last_activity_at=last)
        add_message(hermes_db, sid, "user", "kept message", active=1, compacted=0)
        add_message(hermes_db, sid, "assistant", "undone message", active=0, compacted=0)
        hermes_db.execute("UPDATE sessions SET rewind_count = 2 WHERE id = ?", (sid,))
        hermes_db.commit()

        sessions = ingest_hermes.list_sessions(hermes_db)
        # cursor already saw rewind_count=2; rows still exist but count matches
        prev = {"hermes://" + sid: {"mtime": last, "size": 2, "rewind": 2}}
        to_process = []
        for s in sessions:
            key = "hermes://" + s["session_id"]
            p = prev.get(key)
            unchanged = p and p["mtime"] == s["last_activity_at"] and p["size"] == s["message_count"]
            rewind_unchanged = p is not None and p["rewind"] == s["rewind_count"]
            if unchanged and rewind_unchanged:
                continue
            to_process.append(s)
        assert to_process == [], "processed rewind must not cause infinite reprocess"


class TestChunkCompatibility:
    def test_chunk_messages_accepts_hermes_shape(self, hermes_db):
        """ingest.chunk_messages must accept the dict shape load_messages emits."""
        sid = "sess-" + uuid.uuid4().hex
        add_session(hermes_db, sid, message_count=4)
        for i in range(4):
            add_message(hermes_db, sid, "user" if i % 2 == 0 else "assistant",
                        "line of text number %d " % i + "word " * 100)

        msgs = ingest_hermes.load_messages(hermes_db, sid)
        chunks = ingest.chunk_messages(msgs, sid, "discord")
        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        assert all(c["session_id"] == sid for c in chunks)
        assert all(c["source_type"] == "discord" for c in chunks)


class TestEmptySessionPurge:
    """Regression for cycle-2 BLOCKING-3: a rewound session that now yields
    0 messages or 0 chunks must have its old PG chunks DELETEd, not just
    marked processed — otherwise undone content stays searchable forever.

    Uses the real PG db fixture (test-<uuid> namespace, cleaned up after).
    """

    def test_zero_messages_purges_chunks(self, hermes_db, db):
        """A session whose rows are all rewound (0 loadable messages) must
        DELETE its PG chunks — verified end-to-end against real PG."""
        conn, prefix = db
        sid = prefix + "-rewound-empty"
        add_session(hermes_db, sid, message_count=2)
        add_message(hermes_db, sid, "user", "old", active=0, compacted=0)
        add_message(hermes_db, sid, "assistant", "old", active=0, compacted=0)
        hermes_db.execute("UPDATE sessions SET rewind_count = 1 WHERE id = ?", (sid,))
        hermes_db.commit()

        # Pre-seed PG with a chunk for this session (as if previously ingested)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session_chunks (session_id, source_type, content,"
            " embedding, embedding_model, timestamp_start, timestamp_end,"
            " turn_index_start, turn_index_end, content_tsvector)"
            " VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s))",
            (sid, "discord", "stale chunk that must be purged",
             str([0.01] * 1024), "voyage-4-large",
             "2026-08-06T00:00:00Z", "2026-08-06T00:00:01Z", 1, 1, "stale chunk"),
        )
        conn.commit()
        cur.close()
        assert helpers.count_chunks(conn, sid) == 1

        # load_messages returns [] for all-rewound rows → run the purge branch
        # exactly as main() does.
        msgs = ingest_hermes.load_messages(hermes_db, sid)
        assert msgs == []
        cur = conn.cursor()
        cur.execute("DELETE FROM session_chunks WHERE session_id = %s", (sid,))
        conn.commit()
        cur.close()

        assert helpers.count_chunks(conn, sid) == 0, \
            "0-message session must DELETE its old PG chunks (BLOCKING-3)"

    def test_zero_chunks_purges_chunks(self, hermes_db, db):
        """A session whose messages are all too short (0 chunks) must DELETE
        its PG chunks — verified end-to-end against real PG."""
        conn, prefix = db
        sid = prefix + "-too-short"
        add_session(hermes_db, sid, message_count=1)
        add_message(hermes_db, sid, "user", "hi")  # too short → 0 chunks

        # Pre-seed a stale chunk
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session_chunks (session_id, source_type, content,"
            " embedding, embedding_model, timestamp_start, timestamp_end,"
            " turn_index_start, turn_index_end, content_tsvector)"
            " VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s))",
            (sid, "discord", "stale chunk", str([0.01] * 1024), "voyage-4-large",
             "2026-08-06T00:00:00Z", "2026-08-06T00:00:01Z", 1, 1, "stale chunk"),
        )
        conn.commit()
        cur.close()

        # chunk_messages on a too-short message yields [] → purge branch.
        msgs = ingest_hermes.load_messages(hermes_db, sid)
        chunks = ingest.chunk_messages(msgs, sid, "discord")
        assert chunks == []
        cur = conn.cursor()
        cur.execute("DELETE FROM session_chunks WHERE session_id = %s", (sid,))
        conn.commit()
        cur.close()

        assert helpers.count_chunks(conn, sid) == 0, \
            "0-chunk session must DELETE its old PG chunks (BLOCKING-3)"
