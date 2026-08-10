"""Managed-store privacy lifecycle tests (task #4 successor, DB-backed).

Verifies that export/delete/retention operate ONLY on shiyi's managed rows
(session_chunks / session_facts / ingestion_state), never on the external
source files, and that provenance-based scope resolution fails closed.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from shiyi import privacy  # noqa: E402


def _seed(conn, prefix: str) -> dict[str, str]:
    """Insert managed rows mirroring real ingest naming rules.

    - sessions: session_id = derive_session_id(basename) -> uuid part of file
    - discord:  session_id = 'discord-{stem}'
    - hermes:   session_id bound via file_path 'hermes://<session_id>'
    """
    sessions_sid = f"{prefix}sess-001"
    discord_sid = f"discord-{prefix}general"
    hermes_sid = f"{prefix}hermes-0001"
    with conn.cursor() as cur:
        for sid, stype, content in (
            (sessions_sid, "main_user", "session alpha content"),
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
            "VALUES (%s, now(), 100, 'main_user', 1), (%s, now(), 100, 'discord', 1), (%s, now(), 100, 'main_user', 1)",
            (f"{prefix}path/{prefix}sess-001.jsonl",
             f"{prefix}path/discord-{prefix}general.jsonl",
             f"hermes://{hermes_sid}"),
        )
        conn.commit()
    return {"sessions": sessions_sid, "discord": discord_sid, "hermes": hermes_sid}


def test_scope_resolution_sessions_uses_derive_session_id(db):
    conn, prefix = db
    _seed(conn, prefix)
    sids = privacy.scope_session_ids(conn, "sessions", prefix)
    assert any(sid == f"{prefix}sess-001" for sid in sids)
    assert all(not sid.startswith("discord-") for sid in sids)


def test_scope_resolution_discord_uses_discord_stem(db):
    conn, prefix = db
    _seed(conn, prefix)
    sids = privacy.scope_session_ids(conn, "discord", prefix)
    assert any(sid == f"discord-{prefix}general" for sid in sids)


def test_scope_resolution_hermes_uses_hermes_binding(db):
    conn, prefix = db
    _seed(conn, prefix)
    sids = privacy.scope_session_ids(conn, "hermes", prefix)
    assert any(sid == f"{prefix}hermes-0001" for sid in sids)


def test_scope_unknown_fails_closed(db):
    conn, prefix = db
    with pytest.raises(privacy.PrivacyError):
        privacy.scope_session_ids(conn, "unknown", prefix)


def test_delete_only_removes_selected_scope(db):
    conn, prefix = db
    ids = _seed(conn, prefix)
    result = privacy.delete_scope(conn, "sessions", prefix, confirm=True)
    assert result["deleted_chunks"] > 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["sessions"],))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["discord"],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["hermes"],))
        assert cur.fetchone()[0] == 1


def test_delete_dry_run_writes_nothing(db):
    conn, prefix = db
    ids = _seed(conn, prefix)
    result = privacy.delete_scope(conn, "sessions", prefix, confirm=False)
    assert result["deleted_chunks"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM session_chunks WHERE session_id = %s", (ids["sessions"],))
        assert cur.fetchone()[0] == 1


def test_delete_idempotent(db):
    conn, prefix = db
    _seed(conn, prefix)
    privacy.delete_scope(conn, "sessions", prefix, confirm=True)
    result = privacy.delete_scope(conn, "sessions", prefix, confirm=True)
    assert result["deleted_chunks"] == 0


def test_delete_rolls_back_on_failure(db):
    conn, prefix = db
    _seed(conn, prefix)
    # A scope whose resolution is ambiguous must fail before any deletion.
    with pytest.raises(privacy.PrivacyError):
        privacy.delete_scope(conn, "all", prefix, confirm=True)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM session_chunks")
        assert cur.fetchone()[0] >= 3


def test_export_dry_run_returns_counts_no_write(db, tmp_path):
    conn, prefix = db
    _seed(conn, prefix)
    dest = tmp_path / "export.json"
    result = privacy.export_scope(conn, "sessions", dest, prefix, confirm=False)
    assert result["rows"] > 0
    assert not dest.exists()


def test_export_confirmed_writes_deterministic_artifact(db, tmp_path):
    conn, prefix = db
    _seed(conn, prefix)
    dest = tmp_path / "export.json"
    result = privacy.export_scope(conn, "sessions", dest, prefix, confirm=True)
    assert result["rows"] > 0
    assert dest.exists()
    artifact = json.loads(dest.read_text(encoding="utf-8"))
    assert artifact["scope"] == "sessions"
    assert len(artifact["rows"]) > 0
    privacy.export_scope(conn, "sessions", dest, prefix, confirm=True)
    assert dest.read_bytes() == json.dumps(artifact, ensure_ascii=False, indent=2).encode()


def test_export_confirmed_does_not_touch_source_files(db, tmp_path):
    conn, prefix = db
    _seed(conn, prefix)
    dest = tmp_path / "export.json"
    src = tmp_path / f"{prefix}sess-001.jsonl"
    src.write_text("original bytes\n", encoding="utf-8")
    before = src.read_bytes()
    privacy.export_scope(conn, "sessions", dest, prefix, confirm=True)
    assert src.read_bytes() == before


def test_delete_does_not_touch_source_files(db, tmp_path):
    conn, prefix = db
    _seed(conn, prefix)
    src = tmp_path / f"{prefix}sess-001.jsonl"
    src.write_text("original bytes\n", encoding="utf-8")
    before = src.read_bytes()
    privacy.delete_scope(conn, "sessions", prefix, confirm=True)
    assert src.read_bytes() == before


def test_retention_check_reports_managed_age(db):
    conn, prefix = db
    _seed(conn, prefix)
    report = privacy.retention_check(conn, "sessions", prefix)
    assert "retention_days" in report
    assert report["total"] > 0
    assert "managed_data_age" in report
