"""Repository health, version, and backup/restore contract for shiyi.

Contract (@momoko 70445833):
- ``repository_health`` distinguishes ``uninitialized / partial / current /
  drifted / ahead``; a database whose schema version exceeds the code head is
  ``ahead`` and writes must be rejected.
- backup uses argv-only ``pg_dump`` (no shell), credentials only via env, and
  writes a 0600 temp file + fsync + atomic rename, refusing overwrite and
  symlink targets.  A manifest records format version, schema head/checksum,
  creation time, and a non-sensitive digest.
- restore refuses in-place restore of an existing database.  It restores only
  into a freshly created, random-marker, empty staging database, verifies the
  result, then returns the staging DSN for the user to switch to.  Non-empty /
  unmarked / corrupt targets and bad manifests are rejected; cleanup only ever
  removes the staging database this operation created.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import uuid
from typing import Any

from .migrations import (
    MigrationError,
    _checksum,
    applied_migrations,
    available_migrations,
    code_head,
    migrate,
    schema_version,
)

EXPECTED_TABLES = ("session_chunks", "ingestion_state", "session_facts")
EXPECTED_EXTENSIONS = ("vector", "pg_trgm")
MANIFEST_VERSION = "1"


def _table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s)",
            (name,),
        )
        return bool(cur.fetchone()[0])


def _extension_installed(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = %s)", (name,))
        return bool(cur.fetchone()[0])


def repository_health(conn, *, migrations_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """Report health with an explicit state and required reject flags.

    ``state`` is one of ``uninitialized / partial / current / drifted / ahead``.
    """
    head = code_head(migrations_dir) if migrations_dir else 0
    try:
        version = schema_version(conn)
    except MigrationError:
        version = 0
    tables = {name: _table_exists(conn, name) for name in EXPECTED_TABLES}
    extensions = {name: _extension_installed(conn, name) for name in EXPECTED_EXTENSIONS}
    missing = [n for n, p in tables.items() if not p]
    missing_ext = [n for n, p in extensions.items() if not p]

    drifted = False
    if migrations_dir:
        for a in applied_migrations(conn).values():
            # a.name is the file stem (e.g. "0001_initial"); the file is
            # <name>.py under the migrations dir.
            path = migrations_dir / f"{a.name}.py"
            if not path.is_file():
                drifted = True
                break
            if a.checksum != _checksum(path.read_text(encoding="utf-8")):
                drifted = True
                break

    if head > 0 and version > head:
        state = "ahead"
    elif drifted:
        state = "drifted"
    elif version == 0:
        state = "uninitialized"
    elif missing or missing_ext:
        # Tables/extensions expected for the applied version are absent: this
        # is not a clean "current" state, so writes must fail closed.
        state = "partial"
    elif version < head:
        state = "partial"
    else:
        state = "current"

    ok = state == "current" and not missing and not missing_ext
    # Writes are rejected for every state that is not a clean current DB.
    writes_rejected = state != "current"
    return {
        "ok": ok,
        "state": state,
        "version": version,
        "head": head,
        "tables": tables,
        "extensions": extensions,
        "missing_tables": missing,
        "missing_extensions": missing_ext,
        "writes_rejected": writes_rejected,
    }


def require_writable(conn, *, migrations_dir: pathlib.Path | None = None) -> None:
    """Reject writes to an ahead / non-current database (fail-closed)."""
    health = repository_health(conn, migrations_dir=migrations_dir)
    if health["state"] == "ahead":
        raise MigrationError("database_ahead_of_code", "database schema is ahead of this code head")
    if health["state"] in ("drifted", "partial", "uninitialized"):
        raise MigrationError(
            "database_not_current",
            f"database state is {health['state']}; run `shiyi db migrate` first",
        )


def _connection_dsn_params(conn) -> dict[str, str]:
    info = conn.get_dsn_parameters()
    return {
        "host": info.get("host", "127.0.0.1"),
        "port": info.get("port", "5432"),
        "dbname": info.get("dbname", ""),
        "user": info.get("user", ""),
    }


def _pgpassword(conn) -> str:
    """Extract the DB password without logging it.

    psycopg2 masks the password in .dsn as ``password=xxx``, so prefer the
    original URL/env DSN when present; otherwise fall back to PGPASSWORD.
    """
    dsn = os.environ.get("SHIYI_DATABASE_DSN") or os.environ.get("SHIYI_PG_DSN") or os.environ.get("SHIYI_DATABASE_URL", "")
    if "://" in dsn:
        from urllib.parse import urlsplit

        parts = urlsplit(dsn)
        if parts.password is not None:
            return parts.password
    # libpq key/value form: password=<value> (may be masked as xxx in .dsn)
    raw = getattr(conn, "dsn", "") or ""
    m = __import__("re").search(r"(?:^| )password=([^ ]+)", raw)
    if m and m.group(1) != "xxx":
        return m.group(1)
    return os.environ.get("PGPASSWORD", "")


def _safe_output_path(dest: pathlib.Path) -> None:
    """Reject overwrite, symlink targets, dangling symlinks, symlink directory
    components, and an already-existing sidecar manifest."""
    if dest.exists() or dest.is_symlink():
        raise MigrationError("backup_target_exists", f"backup target already exists: {dest}")
    sidecar = dest.with_suffix(dest.suffix + ".manifest.json")
    if sidecar.exists() or sidecar.is_symlink():
        raise MigrationError(
            "backup_manifest_target_exists",
            f"backup manifest target already exists: {sidecar}",
        )
    # Reject any symlink in the path's existing components (including a
    # dangling symlink that lstat sees but stat would miss).
    cursor = dest.parent
    parts = []
    while cursor != cursor.parent:
        parts.append(cursor)
        cursor = cursor.parent
    for component in reversed(parts):
        if component.is_symlink():
            raise MigrationError("backup_path_symlink", f"backup path traverses a symlink: {component}")
    parent = dest.parent
    if not parent.exists():
        # Destination's parent must already exist; refuse to create through a
        # possibly-symlinked intermediate.
        if not parent.is_dir() and parent.is_symlink():
            raise MigrationError("backup_path_symlink", "backup parent is a symlink")
        raise MigrationError("backup_parent_missing", f"backup parent does not exist: {parent}")


def _run_argv(cmd: list[str], env: dict[str, str]) -> None:
    """Run a child via argv (never a shell).  Failures become stable, redacted
    repository errors; stderr/DSN/credentials are never surfaced."""
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        raise MigrationError(
            "pg_tool_failed",
            f"postgres tool failed: {cmd[0]} (see logs; error redacted)",
        ) from None
    except FileNotFoundError as exc:
        raise MigrationError("pg_tool_missing", f"postgres tool not found: {exc.filename}") from exc


def _atomic_write_0600(path: pathlib.Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def backup(
    conn,
    dest: pathlib.Path,
    *,
    migrations_dir: pathlib.Path | None = None,
    pg_dump: str = "pg_dump",
) -> dict[str, Any]:
    """Backup via argv-only pg_dump into a 0600 temp + atomic rename.

    Credentials are passed only via the environment.  A sidecar manifest
    records format version, schema head/checksum, creation time, and a digest
    of the dump file.  Overwrite/symlink targets are rejected.
    """
    _safe_output_path(dest)
    params = _connection_dsn_params(conn)
    head = code_head(migrations_dir) if migrations_dir else 0
    head_checksum = ""
    if migrations_dir and head > 0:
        names = sorted(available_migrations(migrations_dir).items())
        if names:
            _, (_name, path) = names[-1]
            head_checksum = _checksum(path.read_text(encoding="utf-8"))

    env = dict(os.environ)
    env["PGPASSWORD"] = _pgpassword(conn)
    dump_tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex[:8]}")
    manifest_tmp = dest.with_name(f".{dest.name}.manifest.tmp-{uuid.uuid4().hex[:8]}")
    try:
        # Pre-create the protected 0600 temp target, then let pg_dump write
        # directly to it (bounded by disk, no in-memory stdout payload).
        fd = os.open(dump_tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            subprocess.run(
                [
                    pg_dump,
                    "--format=custom",
                    f"--host={params['host']}",
                    f"--port={params['port']}",
                    f"--username={params['user']}",
                    params["dbname"],
                ],
                env=env,
                check=True,
                stdout=fd,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            raise MigrationError(
                "pg_tool_failed",
                f"postgres tool failed: {pg_dump} (see logs; error redacted)",
            ) from None
        except FileNotFoundError as exc:
            raise MigrationError("pg_tool_missing", f"postgres tool not found: {exc.filename}") from exc
        finally:
            # fsync the fd before close so the temp file is durable.
            try:
                os.fsync(fd)
            except OSError:
                pass
            os.close(fd)
        with open(dump_tmp, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:16]
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "format": "pg_dump-custom",
            "schema_head": head,
            "schema_head_checksum": head_checksum,
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "dump_digest": digest,
        }
        _atomic_write_0600(manifest_tmp, json.dumps(manifest, sort_keys=True))
        os.rename(dump_tmp, dest)
        try:
            os.rename(manifest_tmp, dest.with_suffix(dest.suffix + ".manifest.json"))
        except Exception:
            # Manifest commit failed: remove the dump so no partial backup set
            # (dump without manifest) is left behind.
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass
            raise
    except Exception:
        try:
            os.unlink(dump_tmp)
        except FileNotFoundError:
            pass
        try:
            os.unlink(manifest_tmp)
        except FileNotFoundError:
            pass
        raise
    return {
        "ok": True,
        "path": str(dest),
        "manifest_path": str(dest.with_suffix(dest.suffix + ".manifest.json")),
        "schema_head": head,
        "digest": digest,
    }


def _verify_manifest(manifest_path: pathlib.Path, dump_path: pathlib.Path) -> dict[str, Any]:
    """Validate manifest presence, all required fields, and digest."""
    if not manifest_path.is_file():
        raise MigrationError("manifest_missing", f"backup manifest missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("manifest_corrupt", f"backup manifest unreadable: {exc}") from exc
    required = {
        "manifest_version",
        "format",
        "schema_head",
        "schema_head_checksum",
        "created_at",
        "dump_digest",
    }
    missing_fields = sorted(required - set(manifest))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise MigrationError("manifest_version_unsupported", "unsupported backup manifest version")
    if missing_fields:
        raise MigrationError(
            "manifest_incomplete",
            f"backup manifest missing fields: {missing_fields}",
        )
    if manifest.get("format") != "pg_dump-custom":
        raise MigrationError("manifest_format_unsupported", "unsupported backup format")
    if not dump_path.is_file():
        raise MigrationError("backup_missing", f"backup dump missing: {dump_path}")
    digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()[:16]
    if manifest.get("dump_digest") != digest:
        raise MigrationError("manifest_digest_mismatch", "backup digest does not match manifest")
    return manifest


_SAFE_DB_NAME = __import__("re").compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_db_name(name: str) -> str:
    """Validate a database name with a safe grammar so it can never be
    interpreted as a createdb/dropdb option."""
    if not isinstance(name, str) or not _SAFE_DB_NAME.fullmatch(name):
        raise MigrationError(
            "invalid_db_name",
            "target database name must match ^[a-z][a-z0-9_]{0,62}$",
        )
    return name


def restore(
    conn,
    src: pathlib.Path,
    *,
    target_name: str,
    migrations_dir: pathlib.Path | None = None,
    pg_restore: str = "pg_restore",
) -> dict[str, Any]:
    """Restore a backup into a freshly created, random-marker staging DB.

    Refuses to restore into the current/any existing database.  Creates a new
    empty target with ``target_name``, binds a generated one-time random
    ``marker`` in ``shiyi_restore_guard``, runs argv-only ``pg_restore``, then
    migrates and verifies health.  The manifest schema head/checksum must match
    the current code head.  Returns the staging DSN for the user to switch to.
    Cleanup only ever drops the staging database this call created (identity
    re-verified by marker before any drop).
    """
    manifest = _verify_manifest(src.with_suffix(src.suffix + ".manifest.json"), src)
    if migrations_dir:
        head = code_head(migrations_dir)
        if manifest.get("schema_head") != head:
            raise MigrationError(
                "manifest_schema_mismatch",
                f"backup schema head {manifest.get('schema_head')} does not match code head {head}",
            )
        if head > 0:
            names = sorted(available_migrations(migrations_dir).items())
            if names:
                _, (_name, path) = names[-1]
                head_checksum = _checksum(path.read_text(encoding="utf-8"))
                if manifest.get("schema_head_checksum") != head_checksum:
                    raise MigrationError(
                        "manifest_schema_checksum_mismatch",
                        "backup schema head checksum does not match code head",
                    )

    marker = f"restore-{uuid.uuid4().hex}"
    staging_db = _validate_db_name(target_name)

    # Create a fresh staging database.
    env = dict(os.environ)
    env["PGPASSWORD"] = _pgpassword(conn)
    admin = _connection_dsn_params(conn)
    _run_argv(
        [
            "createdb",
            f"--host={admin['host']}",
            f"--port={admin['port']}",
            f"--username={admin['user']}",
            "--",
            staging_db,
        ],
        env,
    )
    pw = _pgpassword(conn)
    staging_dsn_internal = (
        f"postgresql://{admin['user']}:{pw}@{admin['host']}:{admin['port']}/{staging_db}"
    )
    try:
        import psycopg2

        staging_conn = psycopg2.connect(staging_dsn_internal)
        try:
            with staging_conn.cursor() as cur:
                cur.execute("CREATE TABLE shiyi_restore_guard (marker text PRIMARY KEY)")
                cur.execute("INSERT INTO shiyi_restore_guard(marker) VALUES (%s)", (marker,))
            staging_conn.commit()
        finally:
            staging_conn.close()

        _run_argv(
            [
                pg_restore,
                "--no-owner",
                "--no-privileges",
                f"--host={admin['host']}",
                f"--port={admin['port']}",
                f"--username={admin['user']}",
                "--dbname=" + staging_db,
                str(src),
            ],
            env,
        )

        if migrations_dir:
            staging_conn = psycopg2.connect(staging_dsn_internal)
            try:
                migrate(staging_conn, migrations_dir=migrations_dir)
                health = repository_health(staging_conn, migrations_dir=migrations_dir)
            finally:
                staging_conn.close()
            if not health["ok"] or health["state"] != "current":
                raise MigrationError(
                    "restore_verification_failed",
                    f"restored staging database not healthy: {health['state']}",
                )
        # Return a password-free DSN so the caller can switch to the new DB
        # without ever printing credentials.
        staging_dsn = (
            f"postgresql://{admin['user']}@{admin['host']}:{admin['port']}/{staging_db}"
        )
        return {
            "ok": True,
            "staging_dsn": staging_dsn,
            "marker": marker,
            "schema_head": manifest.get("schema_head"),
        }
    except Exception:
        # Only drop the staging DB this call created: re-verify the marker
        # identity first so a replaced/foreign DB is never dropped.
        try:
            import psycopg2 as _pg2

            check = _pg2.connect(staging_dsn_internal)
            try:
                with check.cursor() as cur:
                    cur.execute("SELECT marker FROM shiyi_restore_guard")
                    row = cur.fetchone()
            finally:
                check.close()
            if row is not None and row[0] == marker:
                _run_argv(
                    ["dropdb", f"--host={admin['host']}", f"--port={admin['port']}",
                     f"--username={admin['user']}", "--", staging_db],
                    env,
                )
        except Exception:
            pass
        raise
