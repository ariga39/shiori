from __future__ import annotations

import json
from pathlib import Path

import pytest

import ingest
import query
from shiyi.cli import main
from shiyi.config import ConfigError, credentials_from_settings, load_config
from shiyi.embeddings import deterministic_embedding as production_embedding
from tests.fake_embeddings import deterministic_embedding


def test_config_has_no_implicit_data_or_secret_paths():
    settings = load_config(environ={})

    assert settings.sessions_dir is None
    assert settings.hermes_db is None
    assert settings.discord_archive_dir is None
    assert settings.pg_cred_file is None
    assert settings.voyage_key_file is None
    assert settings.database_dsn is None


def test_config_priority_is_explicit_then_environment_then_file(tmp_path: Path):
    config_file = tmp_path / "shiyi.toml"
    config_file.write_text(
        '[shiyi]\nsessions_dir = "from-file"\nchunk_tokens = 100\n',
        encoding="utf-8",
    )
    settings = load_config(
        environ={
            "SHIYI_CONFIG_FILE": str(config_file),
            "SHIYI_SESSIONS_DIR": "from-env",
            "SHIYI_CHUNK_TOKENS": "200",
        },
        sessions_dir="from-explicit",
    )

    assert settings.sessions_dir == Path("from-explicit")
    assert settings.chunk_tokens == 200


def test_legacy_paths_require_explicit_switch():
    settings = load_config(environ={}, legacy_openclaw=True)

    assert settings.legacy_openclaw is True
    assert ".openclaw" in str(settings.sessions_dir)
    assert ".openclaw" in str(settings.pg_cred_file)


def test_secret_and_dsn_diagnostics_are_redacted():
    settings = load_config(
        environ={
            "SHIYI_DATABASE_DSN": "postgresql://alice:secret@example.test/db",
            "SHIYI_VOYAGE_API_KEY": "voyage-secret",
        }
    )
    diagnostic = settings.redacted()

    assert "secret" not in json.dumps(diagnostic)
    assert "voyage-secret" not in json.dumps(diagnostic)
    assert "<redacted>" in diagnostic["database_dsn"]


def test_installed_cli_normalizes_key_value_pg_credentials(tmp_path: Path, monkeypatch):
    """The documented SHIYI_PG_CRED shape must reach the CLI as kwargs."""
    cred_file = tmp_path / "postgres.env"
    cred_file.write_text(
        "host=127.0.0.1\nport=5432\ndbname=shiyi\nuser=alice\npassword=synthetic pass\n",
        encoding="utf-8",
    )
    cred_file.chmod(0o600)
    config_file = tmp_path / "shiyi.toml"
    config_file.write_text(
        f'[shiyi]\npg_cred_file = {json.dumps(str(cred_file))}\n',
        encoding="utf-8",
    )
    settings = load_config(config_path=config_file, environ={})
    credentials = credentials_from_settings(settings)
    assert credentials == {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "shiyi",
        "user": "alice",
        "password": "synthetic pass",
    }
    assert settings.redacted()["pg_cred_file"] == "<redacted-path>"

    captured: dict[str, str] = {}

    class FakeConnection:
        def close(self) -> None:
            pass

    def fake_connect(**kwargs: str):
        captured.update(kwargs)
        return FakeConnection()

    import psycopg2

    import shiyi.repository as repository

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    monkeypatch.setattr(
        repository,
        "repository_health",
        lambda conn, migrations_dir=None: {"ok": True, "state": "current"},
    )
    assert main(["--config", str(config_file), "db", "health"]) == 0
    assert captured == credentials


@pytest.mark.parametrize(
    ("contents", "mode", "code"),
    [
        (
            "host=127.0.0.1\nport=5432\ndbname=shiyi\nuser=alice\npassword=one\nhost=other\n",
            0o600,
            "invalid_database_config",
        ),
        (
            "host=127.0.0.1\nport=5432\ndbname=shiyi\nuser=alice\npassword=one\nsslmode=require\n",
            0o600,
            "invalid_database_config",
        ),
        (
            "host=127.0.0.1\nport=not-a-port\ndbname=shiyi\nuser=alice\npassword=one\n",
            0o600,
            "invalid_database_config",
        ),
        (
            "host=127.0.0.1\nport=5432\ndbname=shiyi\nuser=alice\npassword=one\n",
            0o644,
            "credential_file_permissions",
        ),
    ],
)
def test_pg_cred_rejects_unsafe_or_ambiguous_files(tmp_path: Path, contents: str, mode: int, code: str):
    cred_file = tmp_path / "postgres.env"
    cred_file.write_text(contents, encoding="utf-8")
    cred_file.chmod(mode)
    settings = load_config(environ={"SHIYI_PG_CRED": str(cred_file)})

    with pytest.raises(ConfigError) as exc:
        credentials_from_settings(settings)
    assert exc.value.code == code
    assert str(cred_file) not in str(exc.value)


def test_installed_cli_db_and_privacy_lifecycle_share_credential_seam(tmp_path: Path, monkeypatch):
    """All DB/privacy commands reach the connector with validated kwargs."""
    cred_file = tmp_path / "postgres.env"
    cred_file.write_text(
        "host=127.0.0.1\nport=5432\ndbname=shiyi\nuser=alice\npassword=synthetic\n",
        encoding="utf-8",
    )
    cred_file.chmod(0o600)
    config_file = tmp_path / "shiyi.toml"
    config_file.write_text(
        f'[shiyi]\npg_cred_file = {json.dumps(str(cred_file))}\n',
        encoding="utf-8",
    )
    credentials = {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "shiyi",
        "user": "alice",
        "password": "synthetic",
    }
    connections: list[dict[str, str]] = []

    class FakeConnection:
        def close(self) -> None:
            pass

    def fake_connect(**kwargs: str):
        connections.append(kwargs)
        return FakeConnection()

    import psycopg2

    import shiyi.migrations as migrations
    import shiyi.privacy as privacy
    import shiyi.repository as repository

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    monkeypatch.setattr(repository, "repository_health", lambda conn, migrations_dir=None: {"ok": True})
    monkeypatch.setattr(migrations, "migrate", lambda conn, migrations_dir=None: [])
    monkeypatch.setattr(migrations, "schema_version", lambda conn: 1)
    monkeypatch.setattr(
        repository,
        "backup",
        lambda conn, dest, migrations_dir=None: {"ok": True, "path": str(dest), "manifest_path": "m", "schema_head": 1, "digest": "d"},
    )
    monkeypatch.setattr(
        repository,
        "restore",
        lambda conn, src, target_name, migrations_dir=None: {"ok": True, "staging_dsn": "postgresql://alice@host/db", "marker": "m", "schema_head": 1},
    )
    monkeypatch.setattr(privacy, "retention_check", lambda conn, scope, settings: {"ok": True})
    monkeypatch.setattr(
        privacy,
        "export_scope",
        lambda conn, scope, dest, settings, confirm: {"ok": True},
    )
    monkeypatch.setattr(
        privacy,
        "delete_scope",
        lambda conn, scope, settings, confirm, older_than_days: {"ok": True},
    )

    command_lines = [
        ["db", "health"],
        ["db", "migrate"],
        ["db", "backup", str(tmp_path / "backup.dump")],
        ["db", "restore", str(tmp_path / "backup.dump"), "--target", "staging_db"],
        ["privacy", "retention-check", "--scope", "all"],
        ["privacy", "export", "--scope", "all", "--dest", str(tmp_path / "export.json"), "--yes"],
        ["privacy", "delete", "--scope", "all", "--yes"],
    ]
    for command in command_lines:
        assert main(["--config", str(config_file), *command]) == 0
    assert connections == [credentials] * len(command_lines)


def test_missing_embedding_is_structured():
    try:
        load_config(environ={}).require_embedding()
    except ConfigError as exc:
        assert exc.code == "embedding_not_configured"
    else:
        raise AssertionError("missing production embedding config must fail closed")


def test_embedding_dimension_matches_schema():
    settings = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "voyage",
            "SHIYI_VOYAGE_API_KEY": "test-only",
            "SHIYI_VOYAGE_MODEL": "voyage-test",
            "SHIYI_EMBED_DIM": "8",
        }
    )
    try:
        settings.require_embedding()
    except ConfigError as exc:
        assert exc.code == "unsupported_embedding_dimension"
    else:
        raise AssertionError("the vector(1024) schema must reject other dimensions")


def test_fake_embedding_requires_explicit_local_opt_in():
    settings = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "fake",
            "SHIYI_ENVIRONMENT": "development",
            "SHIYI_VOYAGE_MODEL": "shiyi-fake-v1",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    try:
        settings.require_embedding()
    except ConfigError as exc:
        assert exc.code == "fake_embedding_not_allowed"
    else:
        raise AssertionError("fake embeddings must never be enabled implicitly")


def test_explicit_fake_embedding_config_is_local_and_deterministic():
    settings = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "fake",
            "SHIYI_ALLOW_FAKE_EMBEDDINGS": "true",
            "SHIYI_ENVIRONMENT": "test",
            "SHIYI_VOYAGE_MODEL": "shiyi-fake-v1",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    settings.require_embedding()
    first = production_embedding("clean machine smoke", dimension=1024)
    second = production_embedding("clean machine smoke", dimension=1024)
    assert first == second
    assert len(first) == 1024
    assert settings.redacted()["allow_fake_embeddings"] is True


def test_fake_embedding_requires_non_production_environment():
    settings = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "fake",
            "SHIYI_ALLOW_FAKE_EMBEDDINGS": "true",
            "SHIYI_VOYAGE_MODEL": "shiyi-fake-v1",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    try:
        settings.require_embedding()
    except ConfigError as exc:
        assert exc.code == "fake_embedding_environment_required"
    else:
        raise AssertionError("fake embeddings require an explicit non-production environment")


def test_fake_embedding_model_namespace_cannot_cross_provider_boundary():
    fake = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "fake",
            "SHIYI_ALLOW_FAKE_EMBEDDINGS": "true",
            "SHIYI_ENVIRONMENT": "test",
            "SHIYI_VOYAGE_MODEL": "voyage-4-large",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    try:
        fake.require_embedding()
    except ConfigError as exc:
        assert exc.code == "fake_embedding_model_reserved"
    else:
        raise AssertionError("fake vectors require the reserved fake model namespace")

    production = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "voyage",
            "SHIYI_VOYAGE_API_KEY": "synthetic-not-a-key",
            "SHIYI_VOYAGE_MODEL": "shiyi-fake-v1",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    try:
        production.require_embedding()
    except ConfigError as exc:
        assert exc.code == "fake_embedding_model_reserved"
    else:
        raise AssertionError("production queries must reject the fake model namespace")


def test_fake_embedding_contract_is_shared_by_ingest_and_query(monkeypatch):
    settings = load_config(
        environ={
            "SHIYI_EMBEDDING_PROVIDER": "fake",
            "SHIYI_ALLOW_FAKE_EMBEDDINGS": "true",
            "SHIYI_ENVIRONMENT": "test",
            "SHIYI_VOYAGE_MODEL": "shiyi-fake-v1",
            "SHIYI_EMBED_DIM": "1024",
        }
    )
    monkeypatch.setattr(ingest, "VOYAGE_MODEL", "voyage-4-large")
    monkeypatch.setattr(ingest, "EMBED_DIM", 1024)
    monkeypatch.setattr(ingest, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(query, "VOYAGE_MODEL", "voyage-4-large")
    monkeypatch.setattr(query, "EMBED_DIM", 1024)
    monkeypatch.setattr(query, "EMBEDDING_PROVIDER", "voyage")

    ingest.apply_settings(settings)
    query.apply_settings(settings)

    assert ingest.EMBEDDING_PROVIDER == query.EMBEDDING_PROVIDER == "fake"
    assert ingest.VOYAGE_MODEL == query.VOYAGE_MODEL == "shiyi-fake-v1"
    assert ingest.EMBED_DIM == query.EMBED_DIM == 1024


def test_fake_embedding_is_deterministic_and_test_scoped():
    first = deterministic_embedding("offline fixture", dimension=8)
    second = deterministic_embedding("offline fixture", dimension=8)

    assert first == second
    assert first != deterministic_embedding("different fixture", dimension=8)
    assert len(first) == 8


def test_cli_dry_run_requires_explicit_source_but_not_database(tmp_path: Path):
    source = tmp_path / "sessions"
    source.mkdir()
    assert main(["ingest", "--source", "sessions", "--dry-run", "--config", str(tmp_path / "none.toml")]) == 2

    config_file = tmp_path / "valid.toml"
    config_file.write_text(f'[shiyi]\nsessions_dir = "{source}"\n', encoding="utf-8")
    assert main(["--config", str(config_file), "ingest", "--source", "sessions", "--dry-run"]) == 0

    archive_file = tmp_path / "channel.jsonl"
    archive_file.write_text("", encoding="utf-8")
    assert main(["ingest", "--source", "discord", "--file", str(archive_file), "--dry-run"]) == 0
