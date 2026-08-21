"""Schema migrations package.

``structural_snapshot`` parses ``schema.sql`` and the migrated database to
prove the forward-only ``0001_initial`` migration and the legacy ``schema.sql``
never silently diverge on tables, columns, indexes, or extensions.
"""

from __future__ import annotations

import pathlib
import re


def _schema_sql_path() -> pathlib.Path:
    """Return the shipped legacy schema file, package-relative so both source
    trees and installed wheels resolve it."""
    return pathlib.Path(__file__).resolve().parents[1] / "schema.sql"


def _structural_tables() -> set[str]:
    """Return the set of table names declared by the legacy schema.sql."""
    schema = _schema_sql_path()
    text = schema.read_text(encoding="utf-8")
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", text))


def legacy_schema_summary() -> dict[str, object]:
    """Parse schema.sql into a comparable structure.

    Returns ``{tables: {name: [column, ...]}, indexes: [name, ...],
    extensions: [name, ...]}`` using a light regex parse (the file uses a
    stable, hand-maintained layout).
    """
    schema = _schema_sql_path()
    text = schema.read_text(encoding="utf-8")

    tables: dict[str, list[str]] = {}
    current: str | None = None
    column_re = re.compile(r'^\s{4}(?:"([^"]+)"|([a-z_][a-z0-9_]*))\s')
    index_re = re.compile(r"CREATE INDEX IF NOT EXISTS\s+([a-z_]+)")
    extension_re = re.compile(r"CREATE EXTENSION IF NOT EXISTS\s+([a-z_]+)")

    for line in text.splitlines():
        m = re.match(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", line)
        if m:
            table_name: str = m.group(1)
            current = table_name
            tables.setdefault(table_name, [])
            continue
        if current is not None:
            if line.strip() == ");":
                current = None
            else:
                col = column_re.match(line)
                if col:
                    tables.setdefault(current, []).append(col.group(1) or col.group(2))

    indexes = index_re.findall(text)
    extensions = extension_re.findall(text)
    return {"tables": tables, "indexes": sorted(indexes), "extensions": sorted(extensions)}


def migrated_db_summary(conn) -> dict[str, object]:
    """Read the migrated database into the same comparable structure.

    Only the three business tables are compared; migration bookkeeping tables
    (the ``shiori_schema_migrations`` ledger) and test guards are excluded.
    """
    business = {"session_chunks", "ingestion_state", "session_facts"}
    tables: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
        )
        for table, column in cur.fetchall():
            if str(table) not in business:
                continue
            tables.setdefault(str(table), []).append(str(column))
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename IN "
            "('session_chunks','ingestion_state','session_facts') ORDER BY indexname"
        )
        indexes = sorted(str(row[0]) for row in cur.fetchall())
        cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
        extensions = sorted(str(row[0]) for row in cur.fetchall())
    return {"tables": tables, "indexes": indexes, "extensions": extensions}
