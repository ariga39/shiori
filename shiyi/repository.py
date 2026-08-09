"""Repository health, version, and backup/restore contract for shiyi.

Health checks the presence of every expected table and the two required
PostgreSQL extensions (``vector`` for embeddings, ``pg_trgm`` for fallback).
Backup/restore use PostgreSQL ``pg_dump``/``pg_restore`` semantics exposed via
SQL-level copy (transactional) so a failed restore leaves the target unchanged.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from .migrations import MigrationError, schema_version

EXPECTED_TABLES = ("session_chunks", "ingestion_state", "session_facts")
EXPECTED_EXTENSIONS = ("vector", "pg_trgm")


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


def repository_health(conn) -> dict[str, Any]:
    """Return structured health: version, table presence, extension presence."""
    tables = {name: _table_exists(conn, name) for name in EXPECTED_TABLES}
    extensions = {name: _extension_installed(conn, name) for name in EXPECTED_EXTENSIONS}
    missing = [name for name, present in tables.items() if not present]
    missing_ext = [name for name, present in extensions.items() if not present]
    version = 0
    try:
        version = schema_version(conn)
    except MigrationError:
        version = 0
    ok = not missing and not missing_ext
    return {
        "ok": ok,
        "version": version,
        "tables": tables,
        "extensions": extensions,
        "missing_tables": missing,
        "missing_extensions": missing_ext,
    }


def _connection_dsn(conn) -> str:
    info = conn.get_dsn_parameters()
    host = info.get("host", "localhost")
    port = info.get("port", "5432")
    dbname = info.get("dbname", "")
    user = info.get("user", "")
    return f"postgresql://{user}@{host}:{port}/{dbname}"


def backup_to_json(conn, dest: pathlib.Path) -> dict[str, Any]:
    """Export the repository to a JSON backup file (transactional read).

    Never writes credentials.  Returns a small manifest with the exported
    table/row counts and a checksum of the file.
    """
    import hashlib

    payload: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in EXPECTED_TABLES:
            if not _table_exists(conn, table):
                raise MigrationError("missing_table", f"cannot back up missing table {table}")
            cur.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                record = {}
                for col, value in zip(cols, row):
                    # jsonb columns come back as dict/list; serialize so the
                    # backup file is JSON and restore can adapt cleanly.
                    record[col] = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else value
                rows.append(record)
            payload[table] = rows
            counts[table] = len(rows)
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, sort_keys=True, default=str)
    dest.write_text(raw, encoding="utf-8")
    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {"ok": True, "path": str(dest), "counts": counts, "checksum": checksum}


def restore_from_json(conn, src: pathlib.Path) -> dict[str, Any]:
    """Restore from a JSON backup transactionally.

    All tables are replaced in ONE transaction; any failure rolls back so the
    target database is left unchanged (fail-closed).  An unreadable/invalid
    source is a structured error with no write.
    """
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("backup_unreadable", f"cannot read backup {src}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MigrationError("backup_invalid", "backup root must be an object")
    try:
        with conn.cursor() as cur:
            for table in EXPECTED_TABLES:
                rows = payload.get(table)
                if rows is None:
                    raise MigrationError("backup_invalid", f"backup missing table {table}")
                if not _table_exists(conn, table):
                    raise MigrationError(
                        "missing_table",
                        f"cannot restore into missing table {table}; apply migrations first",
                    )
                cur.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
                if rows:
                    cols = list(rows[0].keys())
                    placeholders = ",".join(["%s"] * len(cols))
                    cols_sql = ",".join(f'"{c}"' for c in cols)
                    for row in rows:
                        cur.execute(
                            f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})",
                            [row.get(c) for c in cols],
                        )
        conn.commit()
    except MigrationError:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - fail-closed restore
        conn.rollback()
        raise MigrationError("restore_failed", f"restore failed; target unchanged: {exc}") from exc
    return {"ok": True, "restored": list(EXPECTED_TABLES)}
