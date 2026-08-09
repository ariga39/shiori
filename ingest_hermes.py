#!/usr/bin/env python3
"""
Hermes Session Memory Ingestion (bridge for ~/.hermes/state.db)

Reads Hermes' sqlite session store (sessions + messages tables), filters to
user/assistant text messages, chunks with the same token-based logic as
ingest.py, embeds via Voyage, and stores in the same PostgreSQL
session_chunks table so pg-session-search MCP covers Hermes-era data too.

Design notes:
- Reuses ingest.py functions (chunk_messages, embed_texts_with_retry,
  store_chunks, parse_timestamp) so chunking/embedding/storage stay identical.
- Incremental via ingestion_state: file_path = 'hermes://<session_id>',
  file_mtime = last_activity_at, file_size = message_count.
- Skips cron sessions (same policy as OpenClaw ingest).
- Own advisory lock ID (784322) so it can run in parallel with ingest.py —
  session_id namespaces don't overlap (uuid vs timestamp ids).
"""

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import sqlite3  # uv venv python (3.53.1) — WAL-reset safe

import ingest  # reuse chunk/embed/store logic from the OpenClaw pipeline

HERMES_DB = os.path.expanduser("~/.hermes/state.db")
HERMES_ADVISORY_LOCK_ID = 784322

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/tmp/hermes-ingest.log"),
        logging.StreamHandler(),
    ],
    force=True,
)
log = logging.getLogger("hermes_ingest")

# Source mapping: OpenClaw pipeline uses 'main_user'/'discord'/'subagent'/'cron'.
# Hermes session.source is discord|tui|cron|subagent — map to the same vocabulary.
SOURCE_MAP = {
    "discord": "discord",
    "tui": "main_user",
    "subagent": "subagent",
    "cron": "cron",  # skipped, same as OpenClaw policy
}


def open_hermes_db():
    if not os.path.exists(HERMES_DB):
        raise FileNotFoundError(f"Hermes state.db not found: {HERMES_DB}")
    conn = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_sessions(conn):
    """All sessions with activity info, excluding cron.

    Also reports ``has_rewound`` — whether the session contains any
    soft-archived rewind/undo rows (active=0 AND compacted=0). Hermes
    rewind_to_message() flips rows to active=0 and bumps rewind_count but does
    NOT touch last_activity_at or message_count, so the incremental cursor
    alone can never detect a rewind. Without this signal, undone messages stay
    searchable in PG forever. The probe is cheap (indexed row existence).
    """
    rows = conn.execute(
        """
        SELECT s.id, s.source, s.title, s.chat_id, s.message_count,
               s.started_at, s.last_activity_at, s.rewind_count,
               EXISTS(SELECT 1 FROM messages m
                      WHERE m.session_id = s.id AND m.active = 0
                        AND m.compacted = 0) AS has_rewound
        FROM sessions s
        ORDER BY s.last_activity_at ASC
        """
    ).fetchall()
    result = []
    for r in rows:
        src = SOURCE_MAP.get(r["source"], "main_user")
        if src == "cron":
            continue
        result.append(
            {
                "session_id": r["id"],
                "source_type": src,
                "title": r["title"] or "",
                "chat_id": r["chat_id"] or "",
                "message_count": r["message_count"] or 0,
                "started_at": r["started_at"],
                "last_activity_at": r["last_activity_at"],
                "rewind_count": r["rewind_count"] or 0,
                "has_rewound": bool(r["has_rewound"]),
            }
        )
    return result


def load_messages(conn, session_id):
    """Return user/assistant messages as dicts compatible with ingest.extract_text_from_message.

    Includes BOTH active rows and soft-archived rows (active=0, compacted=1).
    Hermes compaction soft-archives pre-compaction turns (active=0, compacted=1)
    and inserts the summary as new active rows. If we only read active=1 here,
    a re-ingest after compaction would DELETE this session's full-history chunks
    from PG and replace them with the summary only — losing the original detail.
    Excluding (active=0 AND compacted=0) keeps rewind/undo rows out.
    """
    rows = conn.execute(
        """
        SELECT id, role, content, timestamp
        FROM messages
        WHERE session_id = ? AND role IN ('user','assistant')
          AND content IS NOT NULL AND length(content) > 0
          AND (active = 1 OR compacted = 1)
        ORDER BY id ASC
        """,
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "message": {
                    "role": r["role"],
                    "content": r["content"],
                },
                "timestamp": r["timestamp"],  # unix epoch float
                "_line_no": r["id"],  # reuse as turn index
            }
        )
    return out


def get_hermes_state(conn):
    """Return {file_path: {mtime, size, rewind}} from ingestion_state for hermes:// keys.

    processed_offset column stores the session's rewind_count at last ingest
    (the file_size column stores message_count; file_mtime stores
    last_activity_at). Comparing rewind_count lets the cursor detect a rewind
    even when mtime/message_count are unchanged.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT file_path, file_mtime, file_size, processed_offset"
        " FROM ingestion_state WHERE file_path LIKE 'hermes://%'"
    )
    result = {
        row[0]: {"mtime": row[1], "size": row[2], "rewind": row[3] or 0}
        for row in cur.fetchall()
    }
    cur.close()
    return result


def mark_hermes_processed(conn, filepath, mtime, message_count, rewind_count,
                          source_type, chunks_created, partial=False):
    """Mark a Hermes session processed, encoding rewind_count into processed_offset."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_state (file_path, file_mtime, file_size, processed_offset, source_type, chunks_created)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_path) DO UPDATE SET
            file_mtime = EXCLUDED.file_mtime,
            file_size = EXCLUDED.file_size,
            processed_offset = EXCLUDED.processed_offset,
            chunks_created = EXCLUDED.chunks_created,
            processed_at = now()
        """,
        (filepath, mtime, message_count if not partial else 0,
         rewind_count, source_type, chunks_created),
    )
    conn.commit()
    cur.close()


def main():
    parser = argparse.ArgumentParser(description="Hermes session memory ingestion")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Reprocess all sessions")
    parser.add_argument("--session", help="Process only this session id (for testing)")
    args = parser.parse_args()

    log.info("=== Hermes Session Memory Ingestion ===%s", " [DRY-RUN]" if args.dry_run else "")

    locked = False
    db = None
    conn = None
    try:
        if not args.dry_run:
            conn = ingest.get_db()
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (HERMES_ADVISORY_LOCK_ID,))
            locked = cur.fetchone()[0]
            cur.close()
            if not locked:
                log.warning("Another instance running, exiting.")
                return

        db = open_hermes_db()
        sessions = list_sessions(db)
        if args.session:
            sessions = [s for s in sessions if s["session_id"] == args.session]
            if not sessions:
                log.error("Session not found: %s", args.session)
                return

        processed = {} if (args.force or args.dry_run) else get_hermes_state(conn)

        to_process = []
        for s in sessions:
            key = "hermes://" + s["session_id"]
            prev = processed.get(key)
            mtime = datetime.fromtimestamp(s["last_activity_at"], tz=timezone.utc)
            size = s["message_count"]
            unchanged = prev and prev["mtime"] == mtime and prev["size"] == size
            # rewind detection: Hermes rewind flips rows to active=0 and bumps
            # rewind_count WITHOUT touching mtime/message_count, so the
            # mtime+size cursor alone can never detect it. We compare
            # rewind_count instead: if it advanced since last ingest, the
            # session must reprocess so undone messages leave PG.
            #
            # Note: has_rewound (rewind rows present) is deliberately NOT part
            # of the skip predicate — Hermes keeps rewind rows forever (soft
            # delete, no cleanup), so a persistent-rows check would force
            # reprocess on every run forever (infinite churn). rewind_count
            # matching is the reliable "already handled" signal; has_rewound is
            # logged for diagnostics only.
            rewind_unchanged = prev is not None and prev["rewind"] == s["rewind_count"]
            if unchanged and rewind_unchanged:
                continue
            if unchanged:
                log.info("%s: rewind detected (count %d->%d, rows=%s), reprocessing",
                         s["session_id"],
                         prev["rewind"] if prev else 0, s["rewind_count"],
                         s["has_rewound"])
            s["key"] = key
            s["mtime_dt"] = mtime
            to_process.append(s)

        log.info("Found %d sessions, %d already processed, %d to process",
                 len(sessions), len(sessions) - len(to_process), len(to_process))

        total_chunks = 0
        total_sessions = 0
        total_messages = 0
        errors = 0

        if not to_process:
            log.info("Nothing to do.")
            return

        for s in to_process:
            try:
                messages = load_messages(db, s["session_id"])
                if not messages:
                    log.info("%s: no user/assistant messages", s["session_id"])
                    if not args.dry_run:
                        # A rewound session may have zero remaining active rows;
                        # any previously-ingested chunks must be purged so the
                        # undone content stops being searchable in PG.
                        cur = conn.cursor()
                        cur.execute(
                            "DELETE FROM session_chunks WHERE session_id = %s",
                            (s["session_id"],),
                        )
                        conn.commit()
                        cur.close()
                        mark_hermes_processed(
                            conn, s["key"], s["mtime_dt"], s["message_count"],
                            s["rewind_count"], s["source_type"], 0
                        )
                    continue
                total_messages += len(messages)

                chunks = ingest.chunk_messages(messages, s["session_id"], s["source_type"])
                if not chunks:
                    log.info("%s: %d msgs → 0 chunks (all too short)", s["session_id"], len(messages))
                    if not args.dry_run:
                        # Same purge: if nothing chunkable remains, PG must not
                        # keep serving the session's old chunks.
                        cur = conn.cursor()
                        cur.execute(
                            "DELETE FROM session_chunks WHERE session_id = %s",
                            (s["session_id"],),
                        )
                        conn.commit()
                        cur.close()
                        mark_hermes_processed(
                            conn, s["key"], s["mtime_dt"], s["message_count"],
                            s["rewind_count"], s["source_type"], 0
                        )
                    continue

                if args.dry_run:
                    log.info("[DRY-RUN] %s (%s): %d msgs → %d chunks, type=%s",
                             s["session_id"], s["title"], len(messages), len(chunks), s["source_type"])
                    total_chunks += len(chunks)
                    total_sessions += 1
                    continue

                texts = [c["content"] for c in chunks]
                embeddings, failed_indices = ingest.embed_texts_with_retry(texts)

                stored, insert_failed = ingest.store_chunks(
                    chunks, embeddings, failed_indices, conn, fallback_ts=s["started_at"]
                )

                partial = (stored == 0 and len(chunks) > 0) or len(failed_indices) > 0 or insert_failed > 0
                if partial:
                    log.warning("%s: %d embed failures, %d insert failures / %d chunks",
                                s["session_id"], len(failed_indices), insert_failed, len(chunks))

                mark_hermes_processed(
                    conn, s["key"], s["mtime_dt"], s["message_count"],
                    s["rewind_count"], s["source_type"], stored, partial=partial
                )
                total_chunks += stored
                total_sessions += 1

            except Exception as e:
                log.error("Error processing %s: %s", s["session_id"], e)
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                errors += 1
                if errors > 20:
                    log.error("Too many errors, stopping.")
                    break

        log.info("=== Summary (hermes) ===")
        log.info("  Sessions found: %d | Processed: %d | Messages: %d → %d chunks",
                 len(sessions), total_sessions, total_messages, total_chunks)
        log.info("  Errors: %d", errors)
    finally:
        if db is not None:
            db.close()
        try:
            if conn is not None:
                if locked:
                    c = conn.cursor()
                    c.execute("SELECT pg_advisory_unlock(%s)", (HERMES_ADVISORY_LOCK_ID,))
                    c.close()
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
