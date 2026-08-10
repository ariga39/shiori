"""Forward-only PostgreSQL migrations for shiori.

Contract (@momoko 70445833):
- The migration table records ``version/name/checksum/applied_at``.  An applied
  migration whose file checksum changed on disk is fail-closed; migration files
  are forward-only and must never be rewritten after publish.
- ``migrate`` takes a PostgreSQL advisory lock so two concurrent processes run
  migrations serially (never double-applied).
- ``schema_version`` distinguishes the migration table from the head;
  ``repository_health`` reports ``uninitialized / partial / current / drifted /
  ahead``.  A database whose schema version exceeds the code head is ``ahead``
  and writes must be rejected, never treated as healthy.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import re
from dataclasses import dataclass
from typing import cast

_MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
MIGRATIONS_TABLE = "shiori_schema_migrations"
LEGACY_MIGRATIONS_TABLE = "shiyi_schema_migrations"
MIGRATIONS_LOCK_KEY = 784330  # dedicated advisory lock key for schema migrations


class MigrationError(RuntimeError):
    """Structured migration failure with a stable code."""

    def __init__(self, code: str, message: str, *, version: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.version = version


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


def _normalise_version(version: int) -> int:
    if not isinstance(version, int) or version <= 0:
        raise MigrationError("invalid_version", f"migration version must be a positive int, got {version!r}")
    return version


def _load_migration(module_path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        raise MigrationError("migration_unloadable", f"cannot load migration {module_path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s)",
            (LEGACY_MIGRATIONS_TABLE,),
        )
        legacy_exists = bool(cur.fetchone()[0])
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s)",
            (MIGRATIONS_TABLE,),
        )
        canonical_exists = bool(cur.fetchone()[0])
        if legacy_exists and canonical_exists:
            raise MigrationError(
                "migration_table_conflict",
                "both legacy and canonical migration ledger tables exist",
            )
        if legacy_exists:
            # Data-safe forward conversion: an existing database recorded its
            # ledger under the old table name.  Rename it in place so applied
            # migration history is preserved exactly (checksums, versions, and
            # timestamps are untouched).
            cur.execute(
                f"ALTER TABLE {LEGACY_MIGRATIONS_TABLE} RENAME TO {MIGRATIONS_TABLE}"
            )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _applied(conn) -> dict[int, AppliedMigration]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, name, checksum, applied_at FROM {MIGRATIONS_TABLE} ORDER BY version"
        )
        rows = cur.fetchall()
    return {
        int(version): AppliedMigration(
            version=int(version), name=name, checksum=checksum, applied_at=str(applied_at)
        )
        for version, name, checksum, applied_at in rows
    }


def applied_migrations(conn) -> dict[int, AppliedMigration]:
    """Public read of applied migrations (returns {} when table absent)."""
    _ensure_migrations_table(conn)
    return _applied(conn)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _migrations_in(dir_path: pathlib.Path) -> list[tuple[int, pathlib.Path]]:
    if not dir_path.is_dir():
        return []
    result: list[tuple[int, pathlib.Path]] = []
    for entry in dir_path.iterdir():
        m = _MIGRATION_FILE.match(entry.name)
        if m:
            result.append((int(m.group(1)), entry))
    result.sort(key=lambda item: item[0])
    return result


def available_migrations(migrations_dir: pathlib.Path) -> dict[int, tuple[str, pathlib.Path]]:
    """Return {version: (name, path)} for all forward migration files."""
    return {version: (path.stem, path) for version, path in _migrations_in(migrations_dir)}


def code_head(migrations_dir: pathlib.Path) -> int:
    """Highest version shipped in the code (0 if no migration files)."""
    available = available_migrations(migrations_dir)
    return max(available, default=0)


def _legacy_schema_state(conn) -> str:
    """Classify an unversioned database before applying migration 0001.

    ``schema.sql`` predates the migration ledger and is still a supported
    upgrade input.  A complete structural match can safely be adopted into
    the ledger; a partial or drifted legacy schema must not be handed to the
    initial migration, whose DDL intentionally has no overwrite semantics.
    """
    from .schema_migrations import legacy_schema_summary, migrated_db_summary

    expected = legacy_schema_summary()
    actual = migrated_db_summary(conn)
    expected_table_columns = cast(dict[str, list[str]], expected["tables"])
    actual_table_columns = cast(dict[str, list[str]], actual["tables"])
    expected_tables = set(expected_table_columns)
    actual_tables = set(actual_table_columns)
    if not actual_tables:
        return "empty"
    if actual_tables != expected_tables:
        return "unrecognized"
    for table in expected_tables:
        expected_columns = set(expected_table_columns[table])
        actual_columns = set(actual_table_columns[table])
        if actual_columns != expected_columns:
            return "unrecognized"
    expected_indexes = cast(list[str], expected["indexes"])
    actual_indexes = cast(list[str], actual["indexes"])
    if not set(expected_indexes) <= set(actual_indexes):
        return "unrecognized"
    expected_extensions = cast(list[str], expected["extensions"])
    actual_extensions = cast(list[str], actual["extensions"])
    if not set(expected_extensions) <= set(actual_extensions):
        return "unrecognized"
    return "ready"


def schema_version(conn) -> int:
    """Highest applied migration version (0 if table missing/empty)."""
    _ensure_migrations_table(conn)
    applied = _applied(conn)
    return max(applied, default=0)


def migrate(
    conn,
    *,
    migrations_dir: pathlib.Path,
    target: int | None = None,
) -> list[str]:
    """Apply all unapplied forward migrations up to ``target``, serialized.

    Takes an advisory lock for the whole run so concurrent ``migrate`` calls
    never double-apply.  Each migration runs in its own transaction; a failure
    rolls back that migration and leaves previously-applied versions untouched.
    Returns the list of applied migration names.
    """
    _ensure_migrations_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATIONS_LOCK_KEY,))
    try:
        applied = _applied(conn)
        available = available_migrations(migrations_dir)
        if not available:
            raise MigrationError("no_migrations", f"no migration files under {migrations_dir}")
        ordered = sorted(available.items())
        max_available = ordered[-1][0]
        if target is not None:
            _normalise_version(target)
            if target > max_available:
                raise MigrationError(
                    "target_beyond_head",
                    f"target {target} exceeds head {max_available}",
                    version=target,
                )
            ordered = [(v, path) for v, path in ordered if v <= target]

        applied_names: list[str] = []
        # An old database created by schema.sql has the canonical tables but
        # no migration ledger.  Adopt it only after an exact structural
        # check; never rerun the initial CREATE TABLE statements over user
        # data, and never guess when the old schema is partial or drifted.
        if not applied and ordered and ordered[0][0] == 1:
            legacy_state = _legacy_schema_state(conn)
            if legacy_state == "unrecognized":
                raise MigrationError(
                    "legacy_schema_unrecognized",
                    "unversioned database schema is not the supported legacy schema",
                    version=1,
                )
            if legacy_state == "ready":
                version, (name, path) = ordered[0]
                text = path.read_text(encoding="utf-8")
                checksum = _checksum(text)
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum) "
                        "VALUES (%s, %s, %s)",
                        (version, name, checksum),
                    )
                conn.commit()
                applied[version] = AppliedMigration(
                    version=version,
                    name=name,
                    checksum=checksum,
                    applied_at="legacy-adopted",
                )
                applied_names.append(name)
        for version, (name, path) in ordered:
            prior = applied.get(version)
            text = path.read_text(encoding="utf-8")
            checksum = _checksum(text)
            if prior is not None:
                if prior.checksum != checksum:
                    raise MigrationError(
                        "migration_checksum_mismatch",
                        f"already-applied migration {version} changed on disk",
                        version=version,
                    )
                continue
            module = _load_migration(path)
            upgrade = getattr(module, "upgrade", None)
            if not callable(upgrade):
                raise MigrationError("migration_invalid", f"migration {name} has no upgrade()", version=version)
            try:
                with conn.cursor() as cur:
                    upgrade(cur)
                    cur.execute(
                        f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, name, checksum),
                    )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - structured migration failure
                conn.rollback()
                raise MigrationError(
                    "migration_failed",
                    f"migration {name} failed: {exc}",
                    version=version,
                ) from exc
            applied_names.append(name)
        return applied_names
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATIONS_LOCK_KEY,))
        conn.commit()
