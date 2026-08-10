"""Managed-store privacy lifecycle tests (task #4 successor v3, DB-backed).

Uses REAL provenance shapes: absolute source paths, plain discord stems
(general.jsonl -> discord-general), arbitrary hermes session ids — none of which
start with a test/cli prefix. Export/delete/retention act only on managed rows.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from shiori import privacy  # noqa: E402
from shiori.config import Settings  # noqa: E402


@pytest.fixture
def clean_db(db):
    """Real DB with the managed tables emptied for real-shaped isolation.

    The shared ``db`` fixture cleans up by session prefix; these tests use real
    session ids/paths, so the managed tables are truncated before each test.
    """
    conn, _ = db
    with conn.cursor() as cur:
        cur.execute("DELETE FROM session_chunks")
        cur.execute("DELETE FROM session_facts")
        cur.execute("DELETE FROM ingestion_state")
        conn.commit()
    yield conn


def _settings(tmp_path) -> Settings:
    sessions = tmp_path / "sessions"
    discord = tmp_path / "discord"
    sessions.mkdir()
    discord.mkdir()
    (sessions / "abc-123.jsonl").write_text("session alpha\n", encoding="utf-8")
    (sessions / "abc-124.jsonl").write_text("session beta\n", encoding="utf-8")
    (sessions / "def-999.jsonl.deleted.1723").write_text("old\n", encoding="utf-8")
    (discord / "general.jsonl").write_text("discord general\n", encoding="utf-8")
    (discord / "memories.jsonl").write_text("discord memories\n", encoding="utf-8")
    return Settings(
        sessions_dir=sessions,
        discord_archive_dir=discord,
        hermes_db=tmp_path / "hermes" / "state.db",
    )


def _seed(conn, settings) -> dict[str, str]:
    """Insert managed rows mirroring real ingest with absolute paths."""
    sessions_sid1 = "abc-123"
    sessions_sid2 = "abc-124"
    discord_sid = "discord-general"
    hermes_sid = "hermes-session-7f3a"
    with conn.cursor() as cur:
        for sid, stype, content in (
            (sessions_sid1, "main_user", "session alpha content"),
            (sessions_sid2, "main_user", "session beta content"),
            (discord_sid, "discord", "discord general content"),
            (hermes_sid, "main_user", "hermes content"),
        ):
            cur.execute(
                "INSERT INTO session_chunks (session_id, source_type, content, embedding_model, "
                "timestamp_start, timestamp_end, content_tsvector) "
                "VALUES (%s, %s, %s, 'voyage-4-large', now(), now(), to_tsvector('simple', %s))",
                (sid, stype, content, content),
            )
        cur.execute(
            "INSERT INTO ingestion_state (file_path, file_mtime, file_size, source_type, chunks_created) "
            "VALUES (%s, now(), 100, 'main_user', 1), (%s, now(), 100, 'discord', 1), "
            "(%s, now(), 100, 'main_user', 1)",
            (str(settings.sessions_dir / "abc-123.jsonl"),
             str(settings.discord_archive_dir / "general.jsonl"),
             f"hermes://{hermes_sid}"),
        )
        conn.commit()
    return {"sessions": sessions_sid1, "discord": discord_sid, "hermes": hermes_sid}


def test_unimported_file_contributes_zero(clean_db, tmp_path):
    """A source file with no ingestion_state checkpoint is not scope evidence."""
    conn = clean_db
    settings = _settings(tmp_path)
    # abc-124.jsonl and def-999 exist on disk but have NO ingestion_state row.
    _seed(conn, settings)
    bindings = privacy._scope_bindings(conn, settings, "sessions")
    sids = [b.session_id for b in bindings]
    assert "abc-123" in sids
    assert "abc-124" not in sids
    assert not any(s.startswith("def-") for s in sids)


def test_scope_sessions_resolves_from_real_absolute_path(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    bindings = privacy._scope_bindings(conn, settings, "sessions")
    sids = [b.session_id for b in bindings]
    assert "abc-123" in sids
    assert all(not sid.startswith("discord-") for sid in sids)


def test_scope_discord_maps_plain_stem(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    bindings = privacy._scope_bindings(conn, settings, "discord")
    sids = [b.session_id for b in bindings]
    assert "discord-general" in sids
    assert all(sid.startswith("discord-") for sid in sids)


def test_scope_discord_unconditional_prefix(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    double = settings.discord_archive_dir / "discord-general.jsonl"
    double.write_text("x\n", encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_state (file_path, file_mtime, file_size, source_type, chunks_created) "
            "VALUES (%s, now(), 100, 'discord', 1)",
            (str(double),),
        )
        conn.commit()
    bindings = privacy._scope_bindings(conn, settings, "discord")
    sids = [b.session_id for b in bindings]
    assert "discord-discord-general" in sids


def test_scope_hermes_resolves_from_binding(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    bindings = privacy._scope_bindings(conn, settings, "hermes")
    sids = [b.session_id for b in bindings]
    assert "hermes-session-7f3a" in sids


def test_scope_unknown_fails_closed(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    with pytest.raises(privacy.PrivacyError):
        privacy._scope_bindings(conn, settings, "unknown")


def test_all_resolves_union(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    bindings = privacy._resolve_scopes(conn, settings, "all")
    sids = {b.session_id for b in bindings}
    assert {"abc-123", "discord-general", "hermes-session-7f3a"} <= sids


def test_delete_only_removes_selected_scope(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    ids = _seed(conn, settings)
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    assert result["deleted_chunks"] > 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["sessions"],))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["discord"],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["hermes"],))
        assert cur.fetchone()[0] == 1


def test_delete_does_not_touch_other_scope_checkpoints(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM ingestion_state WHERE file_path LIKE %s", ("%discord%",))
        assert cur.fetchone() is not None
        cur.execute("SELECT file_path FROM ingestion_state WHERE file_path LIKE %s", ("%hermes://%",))
        assert cur.fetchone() is not None


def test_delete_dry_run_writes_nothing(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    ids = _seed(conn, settings)
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=False)
    assert result["deleted_chunks"] == 0
    assert result["would_delete_chunks"] > 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["sessions"],))
        assert cur.fetchone()[0] == 1


def test_delete_idempotent(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    assert result["deleted_chunks"] == 0


def test_delete_older_than_narrows_set(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ingestion_state SET processed_at = now() - interval '100 days' "
            "WHERE file_path = %s",
            (str(settings.sessions_dir / "abc-123.jsonl"),),
        )
        conn.commit()
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=False, older_than_days=30)
    assert result["would_delete_chunks"] >= 1
    assert result["would_delete_checkpoints"] == 1


def test_delete_older_than_rejects_nonpositive(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    with pytest.raises(privacy.PrivacyError):
        privacy.delete_scope(conn, "sessions", settings=settings, confirm=False, older_than_days=0)


def test_export_dry_run_returns_counts_no_write(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    dest = tmp_path / "export.json"
    result = privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=False)
    assert result["rows"] > 0
    assert not dest.exists()


def test_export_confirmed_writes_deterministic_artifact(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    dest = tmp_path / "export.json"
    result = privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    assert result["rows"] > 0
    assert dest.exists()
    artifact = json.loads(dest.read_text(encoding="utf-8"))
    assert artifact["scope"] == "sessions"
    # deterministic: exporting again yields identical bytes (already_exported)
    again = privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    assert again["already_exported"] is True


def test_export_public_response_has_no_raw_session_id_or_path(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    dest = tmp_path / "export.json"
    privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    artifact = json.loads(dest.read_text(encoding="utf-8"))
    text = json.dumps(artifact)
    assert "abc-123" not in text  # no raw session id
    assert str(settings.sessions_dir) not in text  # no absolute source path
    for row in artifact["rows"]:
        assert "session" in row  # hashed session, not raw


def test_export_confirmed_does_not_touch_source_files(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    dest = tmp_path / "export.json"
    src = settings.sessions_dir / "abc-123.jsonl"
    before = src.read_bytes()
    privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    assert src.read_bytes() == before


def test_delete_does_not_touch_source_files(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    src = settings.sessions_dir / "abc-123.jsonl"
    before = src.read_bytes()
    privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    assert src.read_bytes() == before


def test_retention_check_reports_managed_age(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    report = privacy.retention_check(conn, "sessions", settings=settings)
    assert "retention_days" in report
    assert report["total"] > 0
    assert "managed_data_age" in report


def test_all_skips_unenabled_sources(clean_db, tmp_path):
    conn = clean_db
    full = _settings(tmp_path)
    _seed(conn, full)
    settings = Settings(sessions_dir=full.sessions_dir)  # discord/hermes unenabled
    bindings = privacy._resolve_scopes(conn, settings, "all")
    sids = [b.session_id for b in bindings]
    assert "abc-123" in sids
    assert not any(s.startswith("discord-") for s in sids)


def test_all_no_configured_sources_fails(clean_db, tmp_path):
    conn = clean_db
    settings = Settings()  # nothing enabled
    with pytest.raises(privacy.PrivacyError) as exc:
        privacy._resolve_scopes(conn, settings, "all")
    assert exc.value.code == "no_configured_sources"


def test_delete_empty_scope_is_idempotent_zero(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    # second delete on an empty (but enabled) scope -> legal 0
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=True)
    assert result["deleted_chunks"] == 0


def test_delete_older_than_keeps_session_with_young_binding(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    # sessions has two sessions; age only ONE checkpoint so that session must be kept
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ingestion_state SET processed_at = now() - interval '100 days' "
            "WHERE file_path = %s",
            (str(settings.sessions_dir / "abc-123.jsonl"),),
        )
        conn.commit()
    # abc-123 is old; abc-124 has no checkpoint row (processed_at NULL) -> kept
    result = privacy.delete_scope(conn, "sessions", settings=settings, confirm=False, older_than_days=30)
    # only abc-123 is fully-old and eligible
    assert result["would_delete_chunks"] == 1
    assert result["would_delete_checkpoints"] == 1


def test_export_recursively_minimizes(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE session_chunks SET content = %s WHERE session_id = %s",
            ("contact a@b.example with token sk_live_abcdef0123456789", "abc-123"),
        )
        conn.commit()
    dest = tmp_path / "export.json"
    privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    text = dest.read_text(encoding="utf-8")
    assert "a@b.example" not in text
    assert "sk_live_abcdef0123456789" not in text


def test_symlink_provenance_fails_closed(clean_db, tmp_path, monkeypatch):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    link = settings.sessions_dir / "abc-123.jsonl"
    link.unlink()
    target = tmp_path / "outside.jsonl"
    target.write_text("x\n", encoding="utf-8")
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink not permitted on this filesystem")
    with pytest.raises(privacy.PrivacyError) as exc:
        privacy._scope_bindings(conn, settings, "sessions")
    assert exc.value.code == "scope_evidence_unavailable"


def test_single_scope_rejects_cross_scope_session_conflict(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    ids = _seed(conn, settings)
    # make hermes claim the same session_id as sessions
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_state (file_path, file_mtime, file_size, source_type, chunks_created) "
            "VALUES (%s, now(), 100, 'main_user', 1)",
            (f"hermes://{ids['sessions']}",),
        )
        conn.commit()
    with pytest.raises(privacy.PrivacyError) as exc:
        privacy._resolve_scopes(conn, settings, "sessions")
    assert exc.value.code == "scope_evidence_unavailable"


def test_export_includes_recursively_minimized_metadata(clean_db, tmp_path):
    conn = clean_db
    settings = _settings(tmp_path)
    _seed(conn, settings)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE session_chunks SET metadata = %s::jsonb WHERE session_id = %s",
            ('{"contact": "a@b.example", "token": "sk_live_0123456789abcdef", "safe": "ok"}', "abc-123"),
        )
        conn.commit()
    dest = tmp_path / "export.json"
    privacy.export_scope(conn, "sessions", dest, settings=settings, confirm=True)
    artifact = json.loads(dest.read_text(encoding="utf-8"))
    meta = artifact["rows"][0]["metadata"]
    text = json.dumps(meta)
    assert "a@b.example" not in text
    assert "sk_live_0123456789abcdef" not in text
    assert "ok" in text  # safe value preserved
