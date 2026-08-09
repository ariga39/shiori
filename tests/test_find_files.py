import os

import pytest

import ingest


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "SESSIONS_DIR", str(tmp_path))
    return tmp_path


def _write(path, content):
    path.write_text(content)
    return str(path)


def test_find_excludes_trajectory_files(session_dir):
    ingest_glob = session_dir / "abc123.jsonl"
    _write(ingest_glob, "user message")
    _write(session_dir / "abc123.trajectory.jsonl", "trajectory data")
    result = ingest.find_session_files()
    assert ingest_glob.name in [os.path.basename(f) for f in result]


def test_find_excludes_bak_and_checkpoint(session_dir):
    main = session_dir / "def456.jsonl"
    _write(main, "content")
    _write(session_dir / "def456.jsonl.bak", "backup")
    _write(session_dir / "def456.checkpoint.jsonl", "checkpoint")
    result = [os.path.basename(f) for f in ingest.find_session_files()]
    assert "def456.jsonl" in result
    assert "def456.jsonl.bak" not in result
    assert "def456.checkpoint.jsonl" not in result


def test_find_single_deleted_file_kept(session_dir):
    _write(session_dir / "ghi789.jsonl.deleted.20260701.jsonl", "old")
    result = [os.path.basename(f) for f in ingest.find_session_files()]
    assert "ghi789.jsonl.deleted.20260701.jsonl" in result


def test_find_deleted_takes_largest_by_size(session_dir):
    _write(session_dir / "abc123.jsonl.deleted.20260701.jsonl", "small")
    _write(session_dir / "abc123.jsonl.deleted.20260702.jsonl", "x" * 5000)
    result = [os.path.basename(f) for f in ingest.find_session_files()]
    assert "abc123.jsonl.deleted.20260702.jsonl" in result
    assert "abc123.jsonl.deleted.20260701.jsonl" not in result


def test_find_active_and_largest_deleted_both_kept(session_dir):
    _write(session_dir / "abc123.jsonl", "active current")
    _write(session_dir / "abc123.jsonl.deleted.20260701.jsonl", "small")
    _write(session_dir / "abc123.jsonl.deleted.20260702.jsonl", "y" * 3000)
    result = [os.path.basename(f) for f in ingest.find_session_files()]
    assert "abc123.jsonl" in result
    assert "abc123.jsonl.deleted.20260702.jsonl" in result
    assert "abc123.jsonl.deleted.20260701.jsonl" not in result


def test_find_sorted_result(session_dir):
    _write(session_dir / "z.jsonl", "z")
    _write(session_dir / "a.jsonl", "a")
    _write(session_dir / "m.jsonl", "m")
    result = ingest.find_session_files()
    names = [os.path.basename(f) for f in result]
    assert names == sorted(names)


# ── derive_session_id ─────────────────────────────────────────────────────────


def test_derive_normal_uuid():
    assert ingest.derive_session_id("/some/dir/abc123.jsonl") == "abc123"


def test_derive_deleted_adds_suffix():
    assert ingest.derive_session_id("/x/abc123.jsonl.deleted.20260701.jsonl") == "abc123:deleted"
