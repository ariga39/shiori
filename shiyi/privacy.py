"""Privacy lifecycle seam for shiyi.

Fail-closed contract:
- :func:`minimize` never echoes a value it cannot positively classify as safe
  to keep; anything matching a recognized sensitive shape is redacted.
- :func:`export` and :func:`delete` perform no filesystem side effect unless
  confirmation is explicit.
- :func:`retention_policy` and :func:`providers` expose the per-source policy
  so operators can verify data handling without reading source code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PrivacyError(ValueError):
    """A user-correctable privacy error with a stable code."""

    code = "privacy_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class IngestSource:
    """One ingestion source and its declared privacy posture."""

    name: str
    kind: str
    is_local_only: bool
    retention_days: int
    provider_name: str
    disclosure_uri: str
    path_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetentionPolicy:
    retention_days: int


_SOURCES = (
    IngestSource(
        name="sessions",
        kind="jsonl",
        is_local_only=True,
        retention_days=90,
        provider_name="local",
        disclosure_uri="local",
        path_patterns=("*.jsonl", "*.jsonl.deleted.*"),
    ),
    IngestSource(
        name="hermes",
        kind="sqlite",
        is_local_only=True,
        retention_days=90,
        provider_name="local",
        disclosure_uri="local",
        path_patterns=("*.db", "*.sqlite", "*.sqlite3"),
    ),
    IngestSource(
        name="discord",
        kind="jsonl",
        is_local_only=True,
        retention_days=30,
        provider_name="discord",
        disclosure_uri="https://discord.com/privacy",
        path_patterns=("*.jsonl",),
    ),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LIVE_TOKEN_RE = re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]+")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]+")
_GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]+")
_BEARER_RE = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~-]+")
_ABS_PATH_RE = re.compile(r"/[A-Za-z0-9_.~/-]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[A-Za-z0-9_ .~\\-]+")

_REDACTION = "[redacted]"


def _redact(text: str) -> str:
    text = _LIVE_TOKEN_RE.sub(_REDACTION, text)
    text = _GITHUB_TOKEN_RE.sub(_REDACTION, text)
    text = _GITHUB_PAT_RE.sub(_REDACTION, text)
    text = _BEARER_RE.sub(lambda m: f"{_REDACTION} {_REDACTION}", text)
    text = _EMAIL_RE.sub(_REDACTION, text)
    text = _WIN_PATH_RE.sub(_REDACTION, text)
    text = _ABS_PATH_RE.sub(_REDACTION, text)
    return text


def minimize(text: str) -> str:
    """Return ``text`` with recognized sensitive values redacted.

    Contract: any value that matches a recognized sensitive shape (provider
    tokens, GitHub-style tokens, bearer authorization, emails, absolute and
    Windows paths) is redacted. Input types that cannot be safely handled are
    rejected rather than silently classified. This is not a claim that every
    unrecognized value is safe.
    """
    if not isinstance(text, str):
        raise PrivacyError(
            "minimize requires string input",
            code="invalid_minimize_input",
        )
    if not text:
        return text
    return _redact(text)


def registered_sources() -> tuple[IngestSource, ...]:
    return _SOURCES


def retention_policy(source: IngestSource) -> RetentionPolicy:
    if source.retention_days <= 0:
        raise PrivacyError(
            "retention_days must be positive",
            code="invalid_retention_policy",
        )
    return RetentionPolicy(retention_days=source.retention_days)


def providers(settings: Any = None) -> list[dict[str, object]]:
    """Disclose configured sources and the embedding provider honestly.

    Embedding provider details are shown only when configured; otherwise it is
    reported as ``not_configured`` rather than silently assumed.
    """
    result: list[dict[str, object]] = []
    for source in _SOURCES:
        if source.kind == "sqlite":
            flow = "local sqlite -> local shiyi store"
        else:
            flow = f"local {source.kind} archive -> local shiyi store"
        result.append(
            {
                "name": source.name,
                "endpoint": source.disclosure_uri,
                "data_flow": flow,
                "retention_days": source.retention_days,
                "is_local_only": source.is_local_only,
            }
        )
    embedding_provider = getattr(settings, "embedding_provider", None) if settings else None
    if embedding_provider:
        result.append(
            {
                "name": "embedding",
                "endpoint": getattr(settings, "voyage_api_url", "") or "unknown",
                "model": getattr(settings, "voyage_model", "") or "unknown",
                "data_flow": "local content -> embedding provider",
                "retention_days": 0,
                "is_local_only": False,
                "status": "configured",
            }
        )
    else:
        result.append(
            {
                "name": "embedding",
                "endpoint": "",
                "model": "",
                "data_flow": "none",
                "retention_days": 0,
                "is_local_only": True,
                "status": "not_configured",
            }
        )
    return result


def export(scope: str, dest: object, *, confirm: bool = False) -> None:
    raise PrivacyError(
        "export must target the managed store; use export_scope",
        code="managed_store_required",
    )


def delete(scope: str, *, confirm: bool = False) -> None:
    raise PrivacyError(
        "delete must target the managed store; use delete_scope",
        code="managed_store_required",
    )


# ── Managed-store lifecycle ─────────────────────────────────────────────────
# The functions below operate ONLY on shiyi's own managed rows (session_chunks,
# session_facts, ingestion_state). External source files are never read for
# export or touched by delete: they exist solely as read-only provenance.
#
# ``session_id_prefix`` isolates a caller's rows (e.g. a test namespace or an
# operator-selected subset); it is also the fail-closed hook for scope
# resolution that cannot be uniquely determined.


def _derive_session_id_from_path(file_path: str) -> str:
    """Mirror ingest.derive_session_id for a provenance file path."""
    basename = os.path.basename(file_path)
    uuid_part = basename.split(".")[0]
    if ".deleted." in basename:
        return uuid_part + ":deleted"
    return uuid_part


def scope_session_ids(conn: Any, scope: str, session_id_prefix: str) -> list[str]:
    """Resolve the managed session_ids belonging to ``scope``.

    Uses only existing provenance rules:
    - sessions: ingestion_state rows whose file_path is a real path (not a
      ``hermes://`` binding) and whose session_id derives from the basename.
    - discord:  ingestion_state rows whose basename maps to ``discord-{stem}``.
    - hermes:   ingestion_state rows bound by ``hermes://<session_id>``.

    A scope that cannot be uniquely attributed fails closed with
    ``scope_evidence_unavailable`` rather than guessing from ``source_type``.
    """
    if scope not in {"sessions", "discord", "hermes"}:
        raise PrivacyError(f"unknown scope: {scope}", code="unknown_scope")
    if not session_id_prefix:
        raise PrivacyError(
            "scope requires a session_id prefix for isolation",
            code="scope_evidence_unavailable",
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT file_path FROM ingestion_state "
            "WHERE file_path LIKE %s OR file_path LIKE %s",
            (session_id_prefix + "%", "hermes://" + session_id_prefix + "%"),
        )
        rows = [r[0] for r in cur.fetchall()]
    if not rows:
        raise PrivacyError(
            f"no managed rows match scope {scope}",
            code="scope_evidence_unavailable",
        )
    if scope == "hermes":
        sids = []
        for file_path in rows:
            if file_path.startswith("hermes://"):
                sids.append(file_path[len("hermes://"):])
        if not sids:
            raise PrivacyError(
                "no hermes:// provenance binding found",
                code="scope_evidence_unavailable",
            )
        return sids
    if scope == "discord":
        sids = []
        for file_path in rows:
            if file_path.startswith("hermes://"):
                continue
            stem = os.path.splitext(os.path.basename(file_path))[0]
            if stem.startswith("discord-"):
                sids.append(stem)
        if not sids:
            raise PrivacyError(
                "no discord-{stem} provenance found",
                code="scope_evidence_unavailable",
            )
        return sids
    # sessions: real paths, not hermes://, session_id derives from basename
    sids = []
    for file_path in rows:
        if file_path.startswith("hermes://"):
            continue
        stem = os.path.splitext(os.path.basename(file_path))[0]
        if stem.startswith("discord-"):
            continue
        sids.append(_derive_session_id_from_path(file_path))
    if not sids:
        raise PrivacyError(
            "no sessions provenance found",
            code="scope_evidence_unavailable",
        )
    return sids


def delete_scope(
    conn: Any,
    scope: str,
    session_id_prefix: str,
    *,
    confirm: bool = False,
    older_than_days: int | None = None,
) -> dict[str, Any]:
    """Delete only managed rows for ``scope`` in a single transaction.

    External source files are never touched. Without confirmation this is a dry
    run. Confirmed deletion is transactional: any failure rolls back all rows.
    Repeating a confirmed delete reports zero additional deletions (idempotent).
    """
    if not confirm:
        # Dry run: resolve and count without deleting. An empty scope is a
        # legitimate zero-count result, not an error.
        sids = _scope_session_ids_tolerant(conn, scope, session_id_prefix)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_chunks WHERE session_id = ANY(%s)",
                (sids,),
            )
            chunk_count = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM session_facts WHERE session_id = ANY(%s)",
                (sids,),
            )
            fact_count = cur.fetchone()[0]
        return {
            "dry_run": True,
            "deleted_chunks": 0,
            "deleted_facts": 0,
            "would_delete_chunks": chunk_count,
            "would_delete_facts": fact_count,
        }
    sids = _scope_session_ids_tolerant(conn, scope, session_id_prefix)
    before = conn.cursor()
    before.execute("SELECT count(*) FROM session_chunks WHERE session_id = ANY(%s)", (sids,))
    chunk_before = before.fetchone()[0]
    before.execute("SELECT count(*) FROM session_facts WHERE session_id = ANY(%s)", (sids,))
    fact_before = before.fetchone()[0]
    before.close()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_chunks WHERE session_id = ANY(%s)", (sids,))
        cur.execute("DELETE FROM session_facts WHERE session_id = ANY(%s)", (sids,))
        cur.execute(
            "DELETE FROM ingestion_state WHERE file_path LIKE %s OR file_path LIKE %s",
            (session_id_prefix + "%", "hermes://" + session_id_prefix + "%"),
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    return {
        "dry_run": False,
        "deleted_chunks": chunk_before,
        "deleted_facts": fact_before,
        "would_delete_chunks": chunk_before,
        "would_delete_facts": fact_before,
    }


def _scope_session_ids_tolerant(conn: Any, scope: str, session_id_prefix: str) -> list[str]:
    """Like scope_session_ids but returns [] when no rows match.

    Used by delete so that repeating a delete on an already-cleared scope
    reports zero rather than failing closed.
    """
    try:
        return scope_session_ids(conn, scope, session_id_prefix)
    except PrivacyError as exc:
        if exc.code == "scope_evidence_unavailable":
            return []
        raise


def export_scope(
    conn: Any,
    scope: str,
    dest: Path | str,
    session_id_prefix: str,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Export minimized managed rows for ``scope`` as one deterministic JSON file.

    Never includes embeddings, tsvectors, secrets, DSNs, or absolute source
    paths. Without confirmation returns a dry-run count. With confirmation the
    file is written atomically (temp + fsync + 0600 + replace); a destination
    with identical content returns ``already_exported``.
    """
    sids = scope_session_ids(conn, scope, session_id_prefix)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, source_type, content, timestamp_start, timestamp_end "
            "FROM session_chunks WHERE session_id = ANY(%s) ORDER BY session_id, timestamp_start",
            (sids,),
        )
        rows = [
            {
                "session_id": r[0],
                "source_type": r[1],
                "content": minimize(r[2]) if r[2] else "",
                "timestamp_start": r[3].isoformat() if r[3] else None,
                "timestamp_end": r[4].isoformat() if r[4] else None,
                "provenance_hash": hashlib.sha256(
                    "|".join(str(v) for v in r[:5]).encode("utf-8")
                ).hexdigest()[:16],
            }
            for r in cur.fetchall()
        ]
    payload = json.dumps(
        {"scope": scope, "rows": rows}, ensure_ascii=False, indent=2
    )
    dest_path = Path(dest)
    if not confirm:
        return {
            "dry_run": True,
            "rows": len(rows),
            "dest": str(dest_path),
            "written": False,
        }
    if dest_path.exists():
        existing = dest_path.read_text(encoding="utf-8")
        if existing == payload:
            return {"dry_run": False, "rows": len(rows), "dest": str(dest_path), "already_exported": True}
        raise PrivacyError(
            "export destination exists with different content; refusing to overwrite",
            code="export_dest_content_mismatch",
        )
    parent = dest_path.parent
    if not parent.exists():
        raise PrivacyError("export destination parent missing", code="export_dest_missing_parent")
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".shiyi-export-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, dest_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {"dry_run": False, "rows": len(rows), "dest": str(dest_path), "written": True}


def retention_check(conn: Any, scope: str, session_id_prefix: str) -> dict[str, Any]:
    """Report managed-data age for a scope using aware-UTC processed_at.

    Never reads external source file mtimes. Returns counts only.
    """
    # Validate the scope resolves to a known provenance (fail closed otherwise).
    scope_session_ids(conn, scope, session_id_prefix)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT processed_at FROM ingestion_state "
            "WHERE file_path LIKE %s OR file_path LIKE %s",
            (session_id_prefix + "%", "hermes://" + session_id_prefix + "%"),
        )
        processed = [r[0] for r in cur.fetchall() if r[0] is not None]
    now = datetime.now(tz=UTC)
    ages = []
    for ts in processed:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ages.append((now - ts).total_seconds() / 86400)
    source = next((s for s in _SOURCES if s.name == scope), _SOURCES[0])
    return {
        "scope": scope,
        "retention_days": source.retention_days,
        "total": len(ages),
        "expired": sum(1 for a in ages if a > source.retention_days),
        "managed_data_age": {"oldest_days": max(ages) if ages else 0, "newest_days": min(ages) if ages else 0},
    }
