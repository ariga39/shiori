#!/usr/bin/env python3
"""
Session Memory Ingestion Pipeline (v2 – Voyage-4-large)

- Reads session JSONL files (active + deleted)
- Filters to user/assistant text only (skips tool/toolResult/system/image)
- Splits into overlapping chunks by **token count** (400 tok / 80 overlap)
- Embeds via Voyage AI (voyage-4-large, 1024-dim)
- Stores in PostgreSQL (pgvector) with tsvector for BM25
"""

import argparse
import glob
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

import psycopg2
import psycopg2.sql
import requests
import tiktoken

from shiori.config import ConfigError, Settings, credentials_from_settings, load_config
from shiori.embeddings import deterministic_embedding
from shiori.privacy import minimize as privacy_minimize

# ── Config ───────────────────────────────────────────────────────────────────
# These compatibility constants are intentionally not data-source defaults.
# The installable CLI configures them from SHIORI_* before production work;
# direct legacy imports may still monkeypatch them in existing tests.
SESSIONS_DIR = None
PG_CRED_PATH = None
DATABASE_DSN = None

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-large"
VOYAGE_KEY_PATH = None
VOYAGE_API_KEY = None
EMBED_DIM = 1024
EMBEDDING_PROVIDER = "voyage"

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 80
VOYAGE_BATCH_SIZE = 128          # Voyage max inputs per call
VOYAGE_RPS_LIMIT = 8            # stay under 10 req/s
EMBED_TIMEOUT = 60
MAX_RETRIES = 3
ADVISORY_LOCK_ID = 784321

# Use cl100k_base (GPT-4 tokenizer) for chunking – close enough for sizing
_enc = tiktoken.get_encoding("cl100k_base")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger(__name__)


def apply_settings(settings: Settings) -> None:
    """Apply typed settings to the legacy module surface.

    Keeping these names lets existing integrations import the old scripts,
    while the CLI and all real commands obtain values from one typed object.
    ``None`` source values do not overwrite compatibility monkeypatches used by
    the old unit tests; real commands fail closed when they try to use them.
    """
    global SESSIONS_DIR, PG_CRED_PATH, DATABASE_DSN
    global VOYAGE_API_URL, VOYAGE_KEY_PATH, VOYAGE_API_KEY, VOYAGE_MODEL, EMBED_DIM, EMBEDDING_PROVIDER
    global CHUNK_TOKENS, CHUNK_OVERLAP, VOYAGE_BATCH_SIZE, VOYAGE_RPS_LIMIT
    global EMBED_TIMEOUT, MAX_RETRIES, ADVISORY_LOCK_ID

    if settings.sessions_dir is not None:
        SESSIONS_DIR = str(settings.sessions_dir)
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
    if settings.embedding_provider is not None:
        EMBEDDING_PROVIDER = settings.embedding_provider
    CHUNK_TOKENS = settings.chunk_tokens
    CHUNK_OVERLAP = settings.chunk_overlap
    VOYAGE_BATCH_SIZE = settings.voyage_batch_size
    VOYAGE_RPS_LIMIT = settings.voyage_rps_limit
    EMBED_TIMEOUT = settings.embed_timeout
    MAX_RETRIES = settings.max_retries
    ADVISORY_LOCK_ID = settings.sessions_lock_id


def configure_logging(settings: Settings) -> None:
    """Add an explicitly configured log file without ever choosing one."""
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
    settings = load_config()
    return settings.read_voyage_key()


def load_credentials(path=None):
    """Load explicitly configured key/value credentials.

    The old implicit home-directory lookup was removed.  Callers may pass a
    path explicitly, or set SHIORI_PG_CRED/SHIORI_DATABASE_DSN.
    """
    if path is not None:
        values = {}
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if "=" in raw_line:
                    k, v = raw_line.split("=", 1)
                    values[k.strip()] = v.strip()
        return values
    if PG_CRED_PATH:
        return load_credentials(PG_CRED_PATH)
    settings = load_config()
    return credentials_from_settings(settings)


def get_db():
    if DATABASE_DSN:
        return psycopg2.connect(DATABASE_DSN)
    creds = load_credentials()
    if "dsn" in creds:
        return psycopg2.connect(creds["dsn"])
    required = ("host", "port", "dbname", "user", "password")
    missing = [key for key in required if key not in creds or not creds[key]]
    if missing:
        raise ConfigError(
            "database credentials missing: " + ", ".join(missing),
            code="invalid_database_config",
        )
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


# ── Session classification ───────────────────────────────────────────────────
def classify_session(filepath, first_lines):
    text = " ".join(first_lines[:20])
    if "Subagent" in text or "SubagentTask" in text or "[Subagent" in text:
        return "subagent"
    if "[cron:" in text or "cron job" in text:
        return "cron"
    return "main_user"


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_session_file(filepath):
    """Return type=message entries from a session JSONL."""
    messages = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            for line_no, raw_line in enumerate(fh, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                obj["_line_no"] = line_no
                messages.append(obj)
    except Exception as e:
        log.warning("Error reading %s: %s", filepath, e)
    return messages


def extract_text_from_message(obj):
    """Extract plain text from user/assistant messages only."""
    msg = obj.get("message", {})
    role = msg.get("role", "")

    # Only keep user and assistant
    if role not in ("user", "assistant"):
        return None

    content = msg.get("content", "")

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                # Skip toolCall, toolResult, image, etc.
            elif isinstance(part, str):
                text_parts.append(part)
        content = "\n".join(text_parts)

    if not content or not isinstance(content, str):
        return None

    # Skip messages that are pure tool call JSON
    if role == "assistant" and content.strip().startswith("{") and '"tool_calls"' in content[:100]:
        return None

    trimmed = content.strip()
    if not trimmed or len(trimmed) < 5:
        return None

    return f"[{role}] {privacy_minimize(trimmed)}"


# ── Token-based chunking ────────────────────────────────────────────────────
def _tokenize(text: str) -> list[int]:
    return _enc.encode(text, disallowed_special=())


def chunk_messages(messages, session_id, source_type):
    """Concatenate all qualifying messages, then chunk by token count."""
    # Collect text items with metadata
    items = []
    for obj in messages:
        text = extract_text_from_message(obj)
        if not text or len(text.strip()) <= 10:
            continue
        items.append({
            "text": text,
            "timestamp": obj.get("timestamp"),
            "index": obj.get("_line_no", 0),
        })

    if not items:
        return []

    # Build a single token stream with item boundaries
    all_text = "\n".join(it["text"] for it in items)
    all_tokens = _tokenize(all_text)

    if not all_tokens:
        return []

    # Precompute character offsets → item index mapping for timestamp lookup
    # We'll map each chunk back to timestamps by char offset
    char_offsets = []  # (start_char, end_char, item_idx)
    pos = 0
    for idx, it in enumerate(items):
        seg = it["text"]
        start = all_text.find(seg, pos)
        if start == -1:
            start = pos
        end = start + len(seg)
        char_offsets.append((start, end, idx))
        pos = end

    total_tokens = len(all_tokens)
    chunks = []
    tok_start = 0

    while tok_start < total_tokens:
        tok_end = min(tok_start + CHUNK_TOKENS, total_tokens)
        chunk_tokens = all_tokens[tok_start:tok_end]
        chunk_text = _enc.decode(chunk_tokens)

        # Find the char range of this chunk in all_text to map timestamps
        char_start = len(_enc.decode(all_tokens[:tok_start]))
        char_end = char_start + len(chunk_text)

        # Find which items overlap this chunk
        overlapping = [
            co for co in char_offsets
            if co[0] < char_end and co[1] > char_start
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
            "source_type": source_type,
            "content": chunk_text,
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
            "turn_index_start": idx_start,
            "turn_index_end": idx_end,
        })

        if tok_end >= total_tokens:
            break
        tok_start += CHUNK_TOKENS - CHUNK_OVERLAP

    return chunks


# ── Voyage embedding ────────────────────────────────────────────────────────
def embed_texts_with_retry(texts):
    """Embed via Voyage API. Returns (embeddings, failed_indices)."""
    if EMBEDDING_PROVIDER == "fake":
        return [deterministic_embedding(text, dimension=EMBED_DIM) for text in texts], []
    api_key = _read_voyage_key()
    all_embeddings = [None] * len(texts)
    failed_indices = []

    for i in range(0, len(texts), VOYAGE_BATCH_SIZE):
        batch_indices = list(range(i, min(i + VOYAGE_BATCH_SIZE, len(texts))))
        batch = [texts[idx][:32000] for idx in batch_indices]  # Voyage input limit

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

        # Rate limiting
        if i + VOYAGE_BATCH_SIZE < len(texts):
            time.sleep(1.0 / VOYAGE_RPS_LIMIT)

    return all_embeddings, failed_indices


# ── Timestamp parsing ────────────────────────────────────────────────────────
def parse_timestamp(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(ts, str):
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
        ]:
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
    return None


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

    session_ids = list({c["session_id"] for c in chunks})
    for sid in session_ids:
        cur.execute("DELETE FROM session_chunks WHERE session_id = %s", (sid,))
        deleted = cur.rowcount
        if deleted:
            log.info("Deleted %d old chunks for session %s", deleted, sid)

    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        if emb is None:  # defensive; unreachable given the guard above
            continue

        ts_start = parse_timestamp(chunk["timestamp_start"])
        ts_end = parse_timestamp(chunk["timestamp_end"])
        if ts_end is None:
            ts_end = ts_start
        # If the message timestamp cannot be parsed, fall back to the file mtime
        # (closer to the message time than INSERT created_at, and stable across
        # re-ingest). Keeps NULL-ts chunks on a time-ordered decay curve.
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
                 content_tsvector)
                VALUES (%s, %s, %s, %s::vector, %s, %s, %s, %s, %s,
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


def find_session_files():
    if not SESSIONS_DIR:
        raise ConfigError(
            "sessions source is disabled; set SHIORI_SESSIONS_DIR",
            code="source_not_configured",
        )
    patterns = [
        os.path.join(SESSIONS_DIR, "*.jsonl"),
        os.path.join(SESSIONS_DIR, "*.jsonl.deleted.*"),
    ]
    raw_files = []
    for pattern in patterns:
        raw_files.extend(glob.glob(pattern))

    filtered = []
    for f in raw_files:
        basename = os.path.basename(f)
        if ".trajectory.jsonl" in basename:
            continue
        if ".checkpoint." in basename:
            continue
        if ".bak" in basename:
            continue
        if basename.endswith(".trajectory-path.json"):
            continue
        filtered.append(f)

    uuid_to_files = {}
    for f in filtered:
        basename = os.path.basename(f)
        uuid = basename.split(".")[0]
        if uuid not in uuid_to_files:
            uuid_to_files[uuid] = []
        uuid_to_files[uuid].append(f)

    result = []
    for uuid, files in uuid_to_files.items():
        if len(files) == 1:
            result.append(files[0])
        else:
            active = [f for f in files if ".deleted." not in os.path.basename(f)]
            deleted = [f for f in files if ".deleted." in os.path.basename(f)]
            if active:
                result.extend(active)
            if deleted:
                result.append(max(deleted, key=lambda x: os.path.getsize(x)))

    return sorted(result)


def derive_session_id(filepath):
    basename = os.path.basename(filepath)
    uuid = basename.split(".")[0]
    if ".deleted." in basename:
        return uuid + ":deleted"
    return uuid


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="Session Memory Ingestion Pipeline (v2)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Reprocess all files")
    parser.add_argument("--config", help="JSON/TOML config file")
    parser.add_argument(
        "--legacy-openclaw",
        action="store_true",
        help="Explicit migration mode: use legacy OpenClaw paths when SHIORI_* is unset",
    )
    args = parser.parse_args(argv)

    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    apply_settings(settings)
    configure_logging(settings)

    log.info("=== Session Memory Ingestion v2 (Voyage) ===%s", " [DRY-RUN]" if args.dry_run else "")

    locked = False
    conn = None
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

        all_files = find_session_files()
        log.info("Found %d session files, %d already processed", len(all_files), len(processed))

        to_process = []
        for filepath in all_files:
            stat = os.stat(filepath)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            size = stat.st_size
            prev = processed.get(filepath)
            if prev and prev["mtime"] == mtime and prev["size"] == size:
                continue
            to_process.append((filepath, mtime, size))

        log.info("Files to process: %d", len(to_process))

        total_chunks = 0
        total_files = 0
        total_messages = 0
        skipped_empty = 0
        skipped_cron = 0
        errors = 0

        if not to_process:
            log.info("Nothing to do.")
            return

        for i, (filepath, mtime, size) in enumerate(to_process):
            if i % 50 == 0 and i > 0:
                log.info("Progress: %d/%d files, %d chunks stored", i, len(to_process), total_chunks)

            try:
                messages = parse_session_file(filepath)
                if not messages:
                    if not args.dry_run:
                        mark_file_processed(conn, filepath, mtime, size, "empty", 0)
                    skipped_empty += 1
                    continue

                total_messages += len(messages)

                first_lines = []
                with open(filepath, encoding="utf-8", errors="replace") as fh:
                    for line_idx, line in enumerate(fh):
                        if line_idx >= 20:
                            break
                        first_lines.append(line[:200])

                source_type = classify_session(filepath, first_lines)
                if source_type == "cron":
                    if not args.dry_run:
                        mark_file_processed(conn, filepath, mtime, size, "cron", 0)
                    skipped_cron += 1
                    continue

                session_id = derive_session_id(filepath)
                chunks = chunk_messages(messages, session_id, source_type)

                if not chunks:
                    if not args.dry_run:
                        mark_file_processed(conn, filepath, mtime, size, source_type, 0)
                    continue

                if args.dry_run:
                    log.info("[DRY-RUN] %s: %d msgs → %d chunks, type=%s",
                             os.path.basename(filepath), len(messages), len(chunks), source_type)
                    total_chunks += len(chunks)
                    total_files += 1
                    continue

                texts = [c["content"] for c in chunks]
                embeddings, failed_indices = embed_texts_with_retry(texts)

                stored, insert_failed = store_chunks(chunks, embeddings, failed_indices, conn, fallback_ts=mtime)

                partial = (stored == 0 and len(chunks) > 0) or len(failed_indices) > 0 or insert_failed > 0
                if partial:
                    log.warning("%s: %d embed failures, %d insert failures / %d chunks",
                                os.path.basename(filepath), len(failed_indices), insert_failed, len(chunks))

                mark_file_processed(conn, filepath, mtime, size, source_type, stored, partial=partial)
                total_chunks += stored
                total_files += 1

            except Exception as e:
                log.error("Error processing %s: %s", filepath, e)
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        # Connection unusable — close it and reconnect. A fresh
                        # connection does NOT inherit the advisory lock, so
                        # re-acquire it or bail: continuing unlocked would let a
                        # concurrent ingest race in on a half-finished session.
                        try:
                            conn.close()
                        except Exception:
                            pass
                        try:
                            conn = get_db()
                        except Exception as reconn_err:
                            log.error("Reconnect failed: %s, aborting", reconn_err)
                            break
                        try:
                            c = conn.cursor()
                            c.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
                            lock_row = c.fetchone()
                            locked = bool(lock_row and lock_row[0])
                            c.close()
                        except Exception as relock_err:
                            log.error("Re-acquire lock failed: %s, aborting", relock_err)
                            break
                        if not locked:
                            log.warning("Another instance took the lock during reconnect, aborting.")
                            break
                errors += 1
                if errors > 20:
                    log.error("Too many errors, stopping.")
                    break

        log.info("=== Summary ===")
        log.info("  Files found: %d | Unchanged: %d | Empty: %d | Cron: %d",
                 len(all_files), len(all_files) - len(to_process), skipped_empty, skipped_cron)
        log.info("  Processed: %d files, %d messages → %d chunks", total_files, total_messages, total_chunks)
        log.info("  Errors: %d", errors)
    finally:
        try:
            if conn is not None:
                if locked:
                    c = conn.cursor()
                    c.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
                    c.close()
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
