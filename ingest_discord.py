#!/usr/bin/env python3
"""
Discord Archive Ingestion Pipeline (v2 – Voyage-4-large)

- Reads Discord archive JSONL files
- Filters to type=0 (normal) and type=19 (reply) messages
- Splits into overlapping chunks by token count (400 tok / 80 overlap)
- Embeds via Voyage AI (voyage-4-large, 1024-dim)
- Stores in PostgreSQL (pgvector) with tsvector for BM25
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
import psycopg2.sql
import requests
import tiktoken

from shiyi.config import ConfigError, Settings, credentials_from_settings, load_config

# ── Config ───────────────────────────────────────────────────────────────────
ARCHIVE_DIR = None
PG_CRED_PATH = None
DATABASE_DSN = None

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-large"
VOYAGE_KEY_PATH = None
VOYAGE_API_KEY = None
EMBED_DIM = 1024

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 80
VOYAGE_BATCH_SIZE = 128
VOYAGE_RPS_LIMIT = 8
EMBED_TIMEOUT = 60
MAX_RETRIES = 3
ADVISORY_LOCK_ID = 784322  # different from ingest.py

ALLOWED_TYPES = {0, 19}
LOG_FILE = None

_enc = tiktoken.get_encoding("cl100k_base")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger(__name__)


def apply_settings(settings: Settings) -> None:
    global ARCHIVE_DIR, PG_CRED_PATH, DATABASE_DSN
    global VOYAGE_API_URL, VOYAGE_KEY_PATH, VOYAGE_API_KEY, VOYAGE_MODEL, EMBED_DIM
    global CHUNK_TOKENS, CHUNK_OVERLAP, VOYAGE_BATCH_SIZE, VOYAGE_RPS_LIMIT
    global EMBED_TIMEOUT, MAX_RETRIES, ADVISORY_LOCK_ID, LOG_FILE

    if settings.discord_archive_dir is not None:
        ARCHIVE_DIR = settings.discord_archive_dir
    if settings.pg_cred_file is not None:
        PG_CRED_PATH = str(settings.pg_cred_file)
    if settings.database_dsn is not None:
        DATABASE_DSN = settings.database_dsn
    if settings.voyage_api_url is not None:
        VOYAGE_API_URL = settings.voyage_api_url
    if settings.voyage_key_file is not None:
        VOYAGE_KEY_PATH = str(settings.voyage_key_file)
    if settings.voyage_api_key is not None:
        VOYAGE_API_KEY = settings.voyage_api_key
    if settings.voyage_model is not None:
        VOYAGE_MODEL = settings.voyage_model
    if settings.embed_dim is not None:
        EMBED_DIM = settings.embed_dim
    if settings.log_file is not None:
        LOG_FILE = str(settings.log_file)
    CHUNK_TOKENS = settings.chunk_tokens
    CHUNK_OVERLAP = settings.chunk_overlap
    VOYAGE_BATCH_SIZE = settings.voyage_batch_size
    VOYAGE_RPS_LIMIT = settings.voyage_rps_limit
    EMBED_TIMEOUT = settings.embed_timeout
    MAX_RETRIES = settings.max_retries
    ADVISORY_LOCK_ID = settings.discord_lock_id


def configure_logging(settings: Settings) -> None:
    if settings.log_file is None:
        return
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(handler, logging.FileHandler) and handler.baseFilename == str(settings.log_file) for handler in log.handlers):
        handler = logging.FileHandler(settings.log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)


# ── Credentials ──────────────────────────────────────────────────────────────
def _read_voyage_key():
    if VOYAGE_API_KEY:
        return VOYAGE_API_KEY
    if VOYAGE_KEY_PATH:
        try:
            with open(VOYAGE_KEY_PATH, encoding="utf-8") as f:
                value = f.read().strip()
        except OSError as exc:
            raise ConfigError("Voyage key file cannot be read", code="key_file_unreadable") from exc
        if not value:
            raise ConfigError("Voyage key file is empty", code="key_file_empty")
        return value
    return load_config().read_voyage_key()


def load_credentials(path=None):
    if path is not None:
        creds = {}
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if "=" in raw_line:
                    k, v = raw_line.split("=", 1)
                    creds[k.strip()] = v.strip()
        return creds
    if PG_CRED_PATH:
        return load_credentials(PG_CRED_PATH)
    return credentials_from_settings(load_config())


def get_db():
    if DATABASE_DSN:
        return psycopg2.connect(DATABASE_DSN)
    creds = load_credentials()
    if "dsn" in creds:
        return psycopg2.connect(creds["dsn"])
    required = ("host", "port", "dbname", "user", "password")
    missing = [key for key in required if key not in creds or not creds[key]]
    if missing:
        raise ConfigError("database credentials missing: " + ", ".join(missing), code="invalid_database_config")
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


# ── Message parsing ─────────────────────────────────────────────────────────
def format_message(msg):
    """Format a Discord message into a text line. Returns None if filtered out."""
    msg_type = msg.get("type", -1)
    if msg_type not in ALLOWED_TYPES:
        return None

    ts_raw = msg.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts_raw)
        ts_str = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        ts_str = ts_raw[:16] if len(ts_raw) >= 16 else ts_raw

    author = msg.get("author", {})
    name = author.get("global_name") or author.get("username") or "unknown"
    content = msg.get("content", "")

    for att in msg.get("attachments", []):
        fname = att.get("filename", "file")
        content += f" [attachment: {fname}]"
    for emb in msg.get("embeds", []):
        title = emb.get("title", "untitled")
        content += f" [embed: {title}]"

    if not content.strip():
        return None

    return f"[{ts_str}] {name}: {content}"


def parse_discord_timestamp(msg):
    ts_raw = msg.get("timestamp", "")
    try:
        return datetime.fromisoformat(ts_raw)
    except Exception:
        return None


def load_messages(jsonl_path):
    messages = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError:
                log.warning("%s:%d invalid JSON, skipping", jsonl_path.name, line_no)
    messages.sort(key=lambda m: m.get("timestamp", ""))
    return messages


# ── Token-based chunking (same as ingest.py) ─────────────────────────────────
def _tokenize(text: str) -> list[int]:
    return _enc.encode(text, disallowed_special=())


def build_chunks(messages, channel_name):
    """Format messages, concatenate, then chunk by token count."""
    items = []
    for i, msg in enumerate(messages):
        text = format_message(msg)
        if text is None:
            continue
        items.append({
            "text": text,
            "timestamp": parse_discord_timestamp(msg),
            "index": i,
            "msg_id": msg.get("id", ""),
        })

    if not items:
        return []

    all_text = "\n".join(it["text"] for it in items)
    all_tokens = _tokenize(all_text)

    if not all_tokens:
        return []

    # Build token offset boundaries per item for efficient lookup
    item_tok_boundaries = []  # (tok_start, tok_end, item_idx)
    cur_pos = 0
    for idx, it in enumerate(items):
        seg_toks = len(_tokenize(it["text"]))
        item_tok_boundaries.append((cur_pos, cur_pos + seg_toks, idx))
        cur_pos += seg_toks
        if idx < len(items) - 1:
            cur_pos += len(_tokenize("\n"))  # separator token

    total_tokens = len(all_tokens)
    chunks = []
    tok_start = 0
    session_id = f"discord-{channel_name}"

    while tok_start < total_tokens:
        tok_end = min(tok_start + CHUNK_TOKENS, total_tokens)
        chunk_tokens = all_tokens[tok_start:tok_end]
        chunk_text = _enc.decode(chunk_tokens)

        # Find items overlapping this token range
        overlapping = [
            b for b in item_tok_boundaries
            if b[0] < tok_end and b[1] > tok_start
        ]

        if overlapping:
            first_item = items[overlapping[0][2]]
            last_item = items[overlapping[-1][2]]
            ts_start = first_item["timestamp"]
            ts_end = last_item["timestamp"]
            idx_start = first_item["index"]
            idx_end = last_item["index"]
        else:
            ts_start = ts_end = items[0]["timestamp"]
            idx_start = idx_end = items[0]["index"]

        chunks.append({
            "session_id": session_id,
            "source_type": "discord",
            "content": chunk_text,
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
            "turn_index_start": idx_start,
            "turn_index_end": idx_end,
            "channel": channel_name,
        })

        if tok_end >= total_tokens:
            break
        tok_start += CHUNK_TOKENS - CHUNK_OVERLAP

    return chunks


# ── Voyage embedding (same as ingest.py) ─────────────────────────────────────
def embed_texts_with_retry(texts):
    """Embed via Voyage API. Returns (embeddings, failed_indices)."""
    api_key = _read_voyage_key()
    all_embeddings = [None] * len(texts)
    failed_indices = []

    for i in range(0, len(texts), VOYAGE_BATCH_SIZE):
        batch_indices = list(range(i, min(i + VOYAGE_BATCH_SIZE, len(texts))))
        batch = [texts[idx][:32000] for idx in batch_indices]

        success = False
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    VOYAGE_API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": VOYAGE_MODEL,
                        "input": batch,
                        "input_type": "document",
                    },
                    timeout=EMBED_TIMEOUT,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 5))
                    log.warning("Voyage 429, sleeping %.1fs", retry_after)
                    time.sleep(retry_after)
                    continue

                resp.raise_for_status()
                data = resp.json()
                emb_list = data.get("data", [])
                if len(emb_list) == len(batch):
                    for j, idx in enumerate(batch_indices):
                        all_embeddings[idx] = emb_list[j]["embedding"]
                    success = True
                    break
                else:
                    log.warning("Voyage batch %d: expected %d, got %d", i, len(batch), len(emb_list))
            except Exception as e:
                wait_time = (2 ** attempt) * 2
                log.warning("Voyage error batch %d (attempt %d/%d): %s. Retry in %ds",
                            i, attempt + 1, MAX_RETRIES, e, wait_time)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait_time)

        if not success:
            log.error("Voyage batch %d FAILED after %d retries", i, MAX_RETRIES)
            failed_indices.extend(batch_indices)

        if i + VOYAGE_BATCH_SIZE < len(texts):
            time.sleep(1.0 / VOYAGE_RPS_LIMIT)

    return all_embeddings, failed_indices


# ── Storage ──────────────────────────────────────────────────────────────────
def store_chunks(chunks, embeddings, failed_indices, conn, fallback_ts=None):
    if not chunks:
        return 0, 0

    failed_set = set(failed_indices)
    cur = conn.cursor()
    stored = 0
    insert_failed = 0

    # Write-ahead atomic semantics (ADR-0001): if ANY chunk's embedding is
    # missing, abort the whole batch — no DELETE, no INSERT — to avoid partial
    # rebuild that destroys memory. Preserve existing data for a later retry.
    missing = [i for i, e in enumerate(embeddings) if i in failed_set or e is None]
    missing += [i for i in failed_indices if i < 0 or i >= len(chunks)]
    if len(embeddings) != len(chunks) or missing:
        log.warning("Batch has %d/%d missing embeddings or length mismatch — preserving existing data",
                    len(missing), len(chunks))
        # no writes happened yet — empty txn, nothing to commit
        cur.close()
        return 0, 0

    # Delete old chunks for these sessions, then re-insert
    session_ids = list({c["session_id"] for c in chunks})
    for sid in session_ids:
        cur.execute("DELETE FROM session_chunks WHERE session_id = %s", (sid,))
        deleted = cur.rowcount
        if deleted:
            log.info("Deleted %d old chunks for session %s", deleted, sid)

    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        if emb is None:  # defensive; unreachable given the guard above
            continue

        ts_start = chunk["timestamp_start"]
        ts_end = chunk["timestamp_end"]
        if ts_end is None:
            ts_end = ts_start
        if ts_start is None and fallback_ts is not None:
            ts_start = fallback_ts
            ts_end = ts_start

        sp_name = psycopg2.sql.Identifier(f"sp_chunk_{idx}")
        try:
            cur.execute(psycopg2.sql.SQL("SAVEPOINT {}").format(sp_name))
            cur.execute("""
                INSERT INTO session_chunks
                (session_id, source_type, content, embedding, embedding_model,
                 timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                 channel, content_tsvector)
                VALUES (%s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s,
                        to_tsvector('simple', %s))
            """, (
                chunk["session_id"],
                chunk["source_type"],
                chunk["content"],
                str(emb),
                VOYAGE_MODEL,
                ts_start,
                ts_end,
                chunk["turn_index_start"],
                chunk["turn_index_end"],
                chunk["channel"],
                chunk["content"],
            ))
            cur.execute(psycopg2.sql.SQL("RELEASE SAVEPOINT {}").format(sp_name))
            stored += 1
        except Exception as e:
            log.error("Insert error chunk %d: %s", idx, e)
            cur.execute(psycopg2.sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sp_name))
            cur.execute(psycopg2.sql.SQL("RELEASE SAVEPOINT {}").format(sp_name))
            insert_failed += 1

    if insert_failed > 0:
        conn.rollback()
        cur.close()
        return 0, insert_failed

    conn.commit()
    cur.close()
    return stored, 0


def get_processed_files(conn):
    cur = conn.cursor()
    cur.execute("SELECT file_path, file_mtime, file_size FROM ingestion_state")
    result = {row[0]: {"mtime": row[1], "size": row[2]} for row in cur.fetchall()}
    cur.close()
    return result


def mark_file_processed(conn, filepath, mtime, size, source_type, chunks_created, partial=False):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ingestion_state (file_path, file_mtime, file_size, processed_offset, source_type, chunks_created)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_path) DO UPDATE SET
            file_mtime = EXCLUDED.file_mtime,
            file_size = EXCLUDED.file_size,
            processed_offset = EXCLUDED.processed_offset,
            chunks_created = EXCLUDED.chunks_created,
            processed_at = now()
    """, (filepath, mtime, size if not partial else 0, size, source_type, chunks_created))
    conn.commit()
    cur.close()


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="Discord Archive Ingestion (v2 – Voyage)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--file", type=str, help="Process only this JSONL file")
    parser.add_argument("--force", action="store_true", help="Reprocess all files")
    parser.add_argument("--config", help="JSON/TOML config file")
    parser.add_argument(
        "--legacy-openclaw",
        action="store_true",
        help="Explicit migration mode: use legacy OpenClaw paths when SHIYI_* is unset",
    )
    args = parser.parse_args(argv)

    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    apply_settings(settings)
    configure_logging(settings)

    log.info("=== Discord Ingest v2 (Voyage) ===%s", " [DRY-RUN]" if args.dry_run else "")

    conn = None
    locked = False

    try:
        if not args.dry_run:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            lock_row = cur.fetchone()
            locked = bool(lock_row and lock_row[0])
            cur.close()
            if not locked:
                log.warning("Another instance running, exiting.")
                return

        processed = {} if (args.force or args.dry_run) else get_processed_files(conn)

        if args.file:
            jsonl_files = [Path(args.file)]
        else:
            if ARCHIVE_DIR is None:
                raise ConfigError(
                    "discord source is disabled; set SHIYI_DISCORD_ARCHIVE_DIR",
                    code="source_not_configured",
                )
            jsonl_files = sorted(ARCHIVE_DIR.glob("*.jsonl"))

        total_msgs = 0
        total_chunks = 0
        total_inserted = 0
        errors = 0

        if not jsonl_files:
            log.error("No JSONL files found in %s", ARCHIVE_DIR)
            return

        for jsonl_path in jsonl_files:
            channel_name = jsonl_path.stem
            filepath = str(jsonl_path.resolve())
            stat = os.stat(jsonl_path)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            size = stat.st_size
            prev = processed.get(filepath)
            if prev and prev["mtime"] == mtime and prev["size"] == size:
                log.info("--- Channel %s: unchanged, skipping", channel_name)
                continue
            log.info("--- Processing channel: %s", channel_name)

            messages = load_messages(jsonl_path)
            log.info("  Loaded %d messages", len(messages))
            total_msgs += len(messages)

            chunks = build_chunks(messages, channel_name)
            log.info("  Built %d chunks (token-based, %d tok / %d overlap)",
                     len(chunks), CHUNK_TOKENS, CHUNK_OVERLAP)
            total_chunks += len(chunks)

            if not chunks:
                if not args.dry_run:
                    mark_file_processed(conn, filepath, mtime, size, channel_name, 0)
                continue

            if args.dry_run:
                for c in chunks[:3]:
                    log.info("  [DRY-RUN] chunk turn=%d-%d tokens≈%d time=%s→%s",
                             c["turn_index_start"], c["turn_index_end"],
                             len(_tokenize(c["content"])),
                             c["timestamp_start"], c["timestamp_end"])
                if len(chunks) > 3:
                    log.info("  [DRY-RUN] ... and %d more chunks", len(chunks) - 3)
                total_inserted += len(chunks)
                continue

            # Embed
            texts = [c["content"] for c in chunks]
            log.info("  Embedding %d chunks via Voyage...", len(texts))
            embeddings, failed_indices = embed_texts_with_retry(texts)

            if failed_indices:
                log.warning("  %d embedding failures", len(failed_indices))

            # Store
            stored, insert_failed = store_chunks(chunks, embeddings, failed_indices, conn, fallback_ts=mtime)
            total_inserted += stored
            log.info("  Channel %s: %d stored, %d failed", channel_name, stored, insert_failed)

            partial = (stored == 0 and len(chunks) > 0) or len(failed_indices) > 0 or insert_failed > 0
            if partial:
                log.warning("  Channel %s PARTIAL: %d embed failures, %d insert failures / %d chunks",
                            channel_name, len(failed_indices), insert_failed, len(chunks))
            mark_file_processed(conn, filepath, mtime, size, channel_name, stored, partial=partial)

            errors += len(failed_indices)
            if insert_failed:
                errors += insert_failed

    finally:
        if conn:
            try:
                if locked:
                    c = conn.cursor()
                    c.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
                    c.close()
                conn.close()
            except Exception:
                pass

    log.info("=== Summary ===")
    log.info("  Total: %d messages → %d chunks → %d inserted", total_msgs, total_chunks, total_inserted)
    log.info("  Errors: %d", errors)


if __name__ == "__main__":
    main()
