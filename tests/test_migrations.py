"""Forward-only migration + repository health/backup/restore tests.

These require the opt-in isolated PostgreSQL test database (see conftest.py
marker contract); they skip when it is not configured.
"""

from __future__ import annotations

import os
import pathlib

import psycopg2
import pytest

from shiyi.migrations import MigrationError, migrate, schema_version
from shiyi.repository import backup_to_json, repository_health, restore_from_json

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "shiyi" / "schema_migrations"

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("SHIYI_TEST_DATABASE_DSN")
        and os.environ.get("SHIYI_TEST_DATABASE_NAME")
        and os.environ.get("SHIYI_TEST_DATABASE_MARKER")
    ),
    reason="isolated PostgreSQL not configured",
)


@pytest.fixture
def conn():
    dsn = os.environ["SHIYI_TEST_DATABASE_DSN"]
    c = psycopg2.connect(dsn)
    yield c
    c.close()


def _reset_schema(conn) -> None:
    """Drop all shiyi tables + the migrations table for a clean test."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS session_chunks CASCADE")
        cur.execute("DROP TABLE IF EXISTS ingestion_state CASCADE")
        cur.execute("DROP TABLE IF EXISTS session_facts CASCADE")
        cur.execute("DROP TABLE IF EXISTS shiyi_schema_migrations CASCADE")
    conn.commit()


def test_migrate_empty_db_to_head(conn):
    _reset_schema(conn)
    applied = migrate(conn, migrations_dir=MIGRATIONS_DIR)
    assert applied  # at least 0001 applied
    assert schema_version(conn) >= 1
    health = repository_health(conn)
    assert health["ok"] is True
    assert all(health["tables"].values())


def test_migrate_is_idempotent(conn):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    v1 = schema_version(conn)
    again = migrate(conn, migrations_dir=MIGRATIONS_DIR)
    assert again == []  # nothing new to apply
    assert schema_version(conn) == v1


def test_schema_version_zero_when_unmigrated(conn):
    _reset_schema(conn)
    assert schema_version(conn) == 0
    health = repository_health(conn)
    assert health["ok"] is False
    assert set(health["missing_tables"]) == {"session_chunks", "ingestion_state", "session_facts"}


def test_repository_health_detects_missing_table(conn):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE session_facts CASCADE")
    conn.commit()
    health = repository_health(conn)
    assert health["ok"] is False
    assert "session_facts" in health["missing_tables"]


def test_checksum_mismatch_of_applied_migration_rejected(conn):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    # Simulate a changed-on-disk applied migration by pointing at a tampered dir.
    tampered = pathlib.Path(os.environ.get("SHIYI_TEST_TMP_DIR", "/tmp")) / "mig"
    tampered.mkdir(parents=True, exist_ok=True)
    (tampered / "0001_initial.py").write_text(
        (MIGRATIONS_DIR / "0001_initial.py").read_text() + "\n# tamper\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError) as exc:
        migrate(conn, migrations_dir=tampered)
    assert exc.value.code == "migration_checksum_mismatch"


def test_failing_migration_rolls_back_and_does_not_advance(conn, tmp_path: pathlib.Path):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    before = schema_version(conn)
    # Add a bad 9999 migration after the head.
    bad_dir = tmp_path / "mig"
    bad_dir.mkdir(parents=True, exist_ok=True)
    for src in MIGRATIONS_DIR.glob("*.py"):
        (bad_dir / src.name).write_text(src.read_text(), encoding="utf-8")
    (bad_dir / "9999_bad.py").write_text(
        "def upgrade(cur):\n    cur.execute('CREATE TABLE bad_migration_test (id int)')\n"
        "    raise RuntimeError('injected failure')\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError) as exc:
        migrate(conn, migrations_dir=bad_dir)
    assert exc.value.code == "migration_failed"
    assert schema_version(conn) == before  # version did not advance
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_tables WHERE tablename='bad_migration_test')")
        assert cur.fetchone()[0] is False  # failed migration rolled back


def test_backup_and_restore_roundtrip(conn, tmp_path: pathlib.Path):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    # Insert one row to make the backup non-trivial.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO session_chunks (session_id, source_type, content, embedding_model) "
            "VALUES ('s1', 'main_user', 'hello', 'voyage-4-large')"
        )
    conn.commit()
    dest = tmp_path / "backup.json"
    backup = backup_to_json(conn, dest)
    assert backup["ok"] is True
    assert backup["counts"]["session_chunks"] == 1
    assert backup["checksum"]

    # Mutate the row, then restore over the existing schema -> original back.
    with conn.cursor() as cur:
        cur.execute("UPDATE session_chunks SET content='changed' WHERE session_id='s1'")
    conn.commit()
    restore = restore_from_json(conn, dest)
    assert restore["ok"] is True
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM session_chunks WHERE session_id='s1'")
        assert cur.fetchone()[0] == "hello"


def test_restore_into_missing_table_fails_closed(conn, tmp_path: pathlib.Path):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    dest = tmp_path / "backup.json"
    backup_to_json(conn, dest)
    # Drop a table -> restore now fails cleanly with missing_table, no write.
    with conn.cursor() as cur:
        cur.execute("DROP TABLE session_chunks CASCADE")
    conn.commit()
    with pytest.raises(MigrationError) as exc:
        restore_from_json(conn, dest)
    assert exc.value.code == "missing_table"
    # session_facts untouched (transaction rolled back, no partial restore).
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_tables WHERE tablename='session_facts')")
        assert cur.fetchone()[0] is True


def test_restore_invalid_source_leaves_target_unchanged(conn, tmp_path: pathlib.Path):
    _reset_schema(conn)
    migrate(conn, migrations_dir=MIGRATIONS_DIR)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO session_chunks (session_id, source_type, content, embedding_model) "
            "VALUES ('keep', 'main_user', 'x', 'm')"
        )
    conn.commit()
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(MigrationError) as exc:
        restore_from_json(conn, bad)
    assert exc.value.code == "backup_unreadable"
    # Target unchanged.
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM session_chunks WHERE session_id='keep'")
        assert cur.fetchone()[0] == "x"


def test_cli_db_migrate_and_health(conn):
    """CLI `shiyi db migrate/health` produce structured output, no credentials."""
    _reset_schema(conn)
    from shiyi.cli import main

    rc = main(["db", "migrate"])
    assert rc == 0
    rc = main(["db", "health"])
    assert rc == 0


def test_cli_db_health_no_credentials_leak(conn, capsys):
    _reset_schema(conn)
    from shiyi.cli import main

    main(["db", "migrate"])
    main(["db", "health"])
    captured = capsys.readouterr()
    assert "shiyi-ci-only" not in captured.out
    assert "shiyi_ci" not in captured.out
