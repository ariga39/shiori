"""Privacy lifecycle seam for shiori.

Fail-closed contract:
- :func:`minimize` redacts every value that matches a recognized sensitive
  shape and rejects input types it cannot safely handle; it does not claim that
  unrecognized values are safe.
- :func:`export` and :func:`delete` act only on the managed store and perform
  filesystem side effects only when confirmation is explicit.
- :func:`retention_policy`, :func:`retention_check`, and :func:`providers`
  expose the per-source policy so operators can verify data handling without
  reading source code.
"""

from __future__ import annotations

import glob
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


def _minimize_value(value: Any) -> Any:
    """Recursively minimize every string in a nested value."""
    if isinstance(value, str):
        return minimize(value)
    if isinstance(value, list):
        return [_minimize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _minimize_value(item) for key, item in value.items()}
    return value


def _json_value(value: Any) -> Any:
    """Return a jsonb value as plain Python objects (or None)."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(value)


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

    Each source reports ``configured`` when its root is set in ``settings`` and
    ``not_configured`` otherwise. Embedding provider details are shown only when
    configured.
    """
    if settings is not None and getattr(settings, "embedding_provider", None) == "fake":
        # Disclosure must not become a side door around the fake provider's
        # environment/opt-in gate.
        settings.require_embedding()
    result: list[dict[str, object]] = []
    for source in _SOURCES:
        if source.kind == "sqlite":
            flow = "local sqlite -> local shiori store"
        else:
            flow = f"local {source.kind} archive -> local shiori store"
        configured = False
        if settings is not None:
            field = {
                "sessions": "sessions_dir",
                "hermes": "hermes_db",
                "discord": "discord_archive_dir",
            }[source.name]
            configured = getattr(settings, field, None) is not None
        result.append(
            {
                "name": source.name,
                "endpoint": source.disclosure_uri,
                "data_flow": flow,
                "retention_days": source.retention_days,
                "is_local_only": source.is_local_only,
                "status": "configured" if configured else "not_configured",
            }
        )
    embedding_provider = getattr(settings, "embedding_provider", None) if settings else None
    if embedding_provider == "fake":
        result.append(
            {
                "name": "embedding",
                "endpoint": "local://deterministic-fake",
                "provider": "deterministic_fake",
                "model": getattr(settings, "voyage_model", "") or "unknown",
                "dimension": getattr(settings, "embed_dim", None),
                "data_flow": "local content -> local deterministic test vector",
                "retention_days": 0,
                "is_local_only": True,
                "external_call": False,
                "environment": getattr(settings, "environment", None),
                "status": "configured_dev_only",
            }
        )
    elif embedding_provider:
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
# The functions below operate ONLY on shiori's own managed rows (session_chunks,
# session_facts, ingestion_state). External source files are never read for
# export or touched by delete: they exist solely as read-only provenance.
#
# Provenance is resolved from the configured source roots and the full
# ingestion_state, yielding an explicit ScopeBinding(file_path, session_id,
# processed_at) per scope. No caller-supplied prefix is accepted: resolution
# must work for real absolute paths, plain discord stems, and arbitrary hermes
# session ids.


@dataclass(frozen=True)
class ScopeBinding:
    """One managed provenance binding for a scope."""

    file_path: str
    session_id: str
    processed_at: datetime | None


def _discover_sessions_files(root: Path) -> list[Path]:
    """Mirror ingest.find_session_files selection rules."""
    patterns = [
        root / "*.jsonl",
        root / "*.jsonl.deleted.*",
    ]
    raw: list[Path] = []
    for pattern in patterns:
        raw.extend(Path(p) for p in glob.glob(str(pattern)))
    filtered = []
    for f in raw:
        basename = f.name
        if ".trajectory.jsonl" in basename:
            continue
        if ".checkpoint." in basename:
            continue
        if ".bak" in basename:
            continue
        if basename.endswith(".trajectory-path.json"):
            continue
        filtered.append(f)
    return filtered


def _derive_session_id_from_path(file_path: str) -> str:
    from .session_ids import derive_session_id as _shared_derive

    return _shared_derive(file_path)


def _scope_bindings(conn: Any, settings: Any, scope: str) -> list[ScopeBinding]:
    """Resolve the managed bindings for ``scope`` from real provenance.

    Sessions and discord are discovered from the configured source roots using
    the real adapter rules; hermes uses the ``hermes://`` ingestion_state
    bindings. Symlinked or out-of-root paths are rejected with
    ``scope_evidence_unavailable``. An enabled scope with no managed rows is a
    legal empty result (delete replays as zero), not an ambiguity.
    """
    if scope not in {"sessions", "discord", "hermes"}:
        raise PrivacyError(f"unknown scope: {scope}", code="unknown_scope")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT file_path, processed_at FROM ingestion_state ORDER BY file_path"
        )
        state_rows = {r[0]: r[1] for r in cur.fetchall()}
    bindings: list[ScopeBinding] = []

    if scope == "hermes":
        hermes_db = getattr(settings, "hermes_db", None)
        if hermes_db is None:
            raise PrivacyError(
                "hermes source is not configured",
                code="source_not_configured",
            )
        for file_path, processed_at in state_rows.items():
            if file_path.startswith("hermes://"):
                bindings.append(
                    ScopeBinding(
                        file_path=file_path,
                        session_id=file_path[len("hermes://"):],
                        processed_at=processed_at,
                    )
                )
        return bindings

    root = getattr(settings, "sessions_dir" if scope == "sessions" else "discord_archive_dir", None)
    if root is None:
        raise PrivacyError(
            f"{scope} source is not configured",
            code="source_not_configured",
        )
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise PrivacyError(
            f"{scope} source root does not exist",
            code="scope_evidence_unavailable",
        )
    from .session_ids import discord_session_id

    for candidate in _discover_sessions_files(root_path) if scope == "sessions" else sorted(root_path.glob("*.jsonl")):
        # Reject a symlink BEFORE dereferencing, then verify containment.
        if candidate.is_symlink():
            raise PrivacyError(
                f"{scope} provenance is a symlink: {candidate}",
                code="scope_evidence_unavailable",
            )
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root_path)
        except ValueError:
            raise PrivacyError(
                f"{scope} provenance outside source root: {resolved}",
                code="scope_evidence_unavailable",
            )
        file_path = str(resolved)
        # A binding is valid only when the exact file path has real
        # ingestion_state provenance. Files that were never imported are not
        # deletion/export evidence and contribute zero.
        if file_path not in state_rows:
            continue
        if scope == "discord":
            session_id = discord_session_id(candidate.stem)
        else:
            session_id = _derive_session_id_from_path(file_path)
        bindings.append(
            ScopeBinding(
                file_path=file_path,
                session_id=session_id,
                processed_at=state_rows.get(file_path),
            )
        )
    return bindings


def _resolve_scopes(conn: Any, settings: Any, scope: str) -> list[ScopeBinding]:
    """Resolve one scope or the atomic union ``all``.

    ``all`` expands only the sources explicitly enabled in ``settings``; an
    unenabled source is not a failure. If no source is enabled at all it fails
    with ``no_configured_sources``. Real ambiguity (a session id or file path
    claimed by more than one scope) fails the whole operation with zero side
    effects; enabled-but-empty scopes contribute nothing.
    """
    if scope == "all":
        all_bindings: list[ScopeBinding] = []
        enabled = 0
        for name in ("sessions", "discord", "hermes"):
            try:
                all_bindings.extend(_scope_bindings(conn, settings, name))
                enabled += 1
            except PrivacyError as exc:
                if exc.code == "source_not_configured":
                    continue
                raise
        if enabled == 0:
            raise PrivacyError(
                "no configured source is enabled",
                code="no_configured_sources",
            )
        seen_session: dict[str, str] = {}
        seen_path: dict[str, str] = {}
        for b in all_bindings:
            if b.session_id in seen_session and seen_session[b.session_id] != b.file_path:
                raise PrivacyError(
                    f"session {b.session_id!r} is claimed by more than one scope",
                    code="scope_evidence_unavailable",
                )
            seen_session[b.session_id] = b.file_path
            if b.file_path in seen_path and seen_path[b.file_path] != b.session_id:
                raise PrivacyError(
                    f"file path {b.file_path!r} maps to more than one session",
                    code="scope_evidence_unavailable",
                )
            seen_path[b.file_path] = b.session_id
        return all_bindings
    bindings = _scope_bindings(conn, settings, scope)
    # A single-scope operation must still fail closed if its session ids are
    # claimed by another enabled scope (chunks/facts key only on session_id, so
    # deleting one scope would silently remove the other's rows).
    claimed: dict[str, str] = {b.session_id: b.file_path for b in bindings}
    for name in ("sessions", "discord", "hermes"):
        if name == scope:
            continue
        try:
            other = _scope_bindings(conn, settings, name)
        except PrivacyError as exc:
            if exc.code == "source_not_configured":
                continue
            raise
        for b in other:
            if b.session_id in claimed:
                raise PrivacyError(
                    f"session {b.session_id!r} is claimed by both {scope} and {name}",
                    code="scope_evidence_unavailable",
                )
    return bindings


def _session_ids(bindings: list[ScopeBinding]) -> list[str]:
    return sorted({b.session_id for b in bindings})


def _checkpoint_paths(bindings: list[ScopeBinding]) -> list[str]:
    return [b.file_path for b in bindings]


def delete_scope(
    conn: Any,
    scope: str,
    *,
    settings: Any,
    confirm: bool = False,
    older_than_days: int | None = None,
) -> dict[str, Any]:
    """Delete only managed rows for ``scope`` in a single transaction.

    External source files are never touched. Without confirmation this is a dry
    run. Confirmed deletion is transactional: any failure rolls back all rows.
    Repeating a confirmed delete reports zero additional deletions.
    """
    if older_than_days is not None and older_than_days <= 0:
        raise PrivacyError(
            "older_than_days must be a positive integer",
            code="invalid_older_than",
        )
    bindings = _resolve_scopes(conn, settings, scope)
    if older_than_days is not None:
        now = datetime.now(tz=UTC)
        # Group by session: a session is only eligible when EVERY one of its
        # bindings is older than the threshold, so an old checkpoint cannot
        # carry away still-active data for the same session.
        by_session: dict[str, list[ScopeBinding]] = {}
        for b in bindings:
            by_session.setdefault(b.session_id, []).append(b)

        def _session_old_enough(session_bindings: list[ScopeBinding]) -> bool:
            if not session_bindings:
                return False
            for b in session_bindings:
                if b.processed_at is None:
                    return False
                ts = b.processed_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if (now - ts).total_seconds() / 86400 <= older_than_days:
                    return False
            return True

        kept = []
        for session_bindings in by_session.values():
            if _session_old_enough(session_bindings):
                kept.extend(session_bindings)
        bindings = kept
    sids = _session_ids(bindings)
    paths = _checkpoint_paths(bindings)
    if not confirm:
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
            cur.execute(
                "SELECT count(*) FROM ingestion_state WHERE file_path = ANY(%s)",
                (paths,),
            )
            checkpoint_count = cur.fetchone()[0]
        return {
            "dry_run": True,
            "deleted_chunks": 0,
            "deleted_facts": 0,
            "deleted_checkpoints": 0,
            "would_delete_chunks": chunk_count,
            "would_delete_facts": fact_count,
            "would_delete_checkpoints": checkpoint_count,
        }
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_chunks WHERE session_id = ANY(%s)", (sids,))
        chunk_deleted = cur.rowcount
        cur.execute("DELETE FROM session_facts WHERE session_id = ANY(%s)", (sids,))
        fact_deleted = cur.rowcount
        cur.execute("DELETE FROM ingestion_state WHERE file_path = ANY(%s)", (paths,))
        checkpoint_deleted = cur.rowcount
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    return {
        "dry_run": False,
        "deleted_chunks": chunk_deleted,
        "deleted_facts": fact_deleted,
        "deleted_checkpoints": checkpoint_deleted,
        "would_delete_chunks": len(sids),
        "would_delete_facts": 0,
        "would_delete_checkpoints": len(paths),
    }


def export_scope(
    conn: Any,
    scope: str,
    dest: Path | str,
    *,
    settings: Any,
    confirm: bool = False,
) -> dict[str, Any]:
    """Export minimized managed rows for ``scope`` as one deterministic JSON file.

    Never includes embeddings, tsvectors, secrets, DSNs, or absolute source
    paths; public fields use session hashes, never raw session ids or paths.
    Without confirmation returns a dry-run count. With confirmation the file is
    written atomically (temp + fsync + 0600 + replace); a destination with
    identical content returns ``already_exported``.
    """
    bindings = _resolve_scopes(conn, settings, scope)
    sids = _session_ids(bindings)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, session_id, source_type, content, timestamp_start, timestamp_end, metadata "
            "FROM session_chunks WHERE session_id = ANY(%s) "
            "ORDER BY session_id, timestamp_start, content, id",
            (sids,),
        )
        chunk_rows = cur.fetchall()
        cur.execute(
            "SELECT id, session_id, category, content, \"timestamp\", task_summary, metadata "
            "FROM session_facts WHERE session_id = ANY(%s) "
            "ORDER BY session_id, category, \"timestamp\", content, id",
            (sids,),
        )
        fact_rows = cur.fetchall()
    rows = []
    for r in chunk_rows:
        rows.append(
            {
                "kind": "chunk",
                "session": hashlib.sha256((r[1] or "").encode("utf-8")).hexdigest()[:16],
                "source_type": _minimize_value(r[2]),
                "content": _minimize_value(r[3]),
                "timestamp_start": r[4].isoformat() if r[4] else None,
                "timestamp_end": r[5].isoformat() if r[5] else None,
                "metadata": _minimize_value(_json_value(r[6])),
            }
        )
    for r in fact_rows:
        rows.append(
            {
                "kind": "fact",
                "session": hashlib.sha256((r[1] or "").encode("utf-8")).hexdigest()[:16],
                "category": _minimize_value(r[2]),
                "content": _minimize_value(r[3]),
                "timestamp": r[4].isoformat() if r[4] else None,
                "task_summary": _minimize_value(r[5]),
                "metadata": _minimize_value(_json_value(r[6])),
            }
        )
    payload = json.dumps({"scope": scope, "rows": rows}, ensure_ascii=False, indent=2)
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
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".shiori-export-")
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


def retention_check(conn: Any, scope: str, *, settings: Any) -> dict[str, Any]:
    """Report managed-data age for a scope using aware-UTC processed_at.

    Never reads external source file mtimes. Returns counts only.
    """
    bindings = _resolve_scopes(conn, settings, scope)
    source = next((s for s in _SOURCES if s.name == scope), _SOURCES[0])
    now = datetime.now(tz=UTC)
    ages = []
    for b in bindings:
        if b.processed_at is None:
            continue
        ts = b.processed_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ages.append((now - ts).total_seconds() / 86400)
    return {
        "scope": scope,
        "retention_days": source.retention_days,
        "total": len(ages),
        "expired": sum(1 for a in ages if a > source.retention_days),
        "managed_data_age": {
            "oldest_days": max(ages) if ages else 0,
            "newest_days": min(ages) if ages else 0,
        },
    }
