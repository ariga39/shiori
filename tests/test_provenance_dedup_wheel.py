"""Task #33 — installed-wheel CLI public characterization for provenance dedup.

Builds the current tree into a wheel offline, installs it into a clean venv
without network/model access, then verifies the installed CLI ``query``
preserves distinct-evidence ordering (current fact B before historical fact A)
through the real PostgreSQL seam.  No source-tree module is loaded by the child;
``query`` must resolve from the wheel's site-packages only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "replay_provenance_dedup"
MODEL_IDENTITY = "voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f"
EMBED_DIM = 1024

# Frozen pair (A/B): cosine(A, B) = 0.9452 > 0.85, byte-different content.
A = "[user] The deadline for the quarterly report was moved to the end of August."
B = "[user] The quarterly report deadline is now the last working day of August."
QUERY = "when is the quarterly report deadline?"


@pytest.fixture(scope="module")
def uv() -> str:
    found = shutil.which("uv")
    if not found:
        pytest.skip("uv is required to build/install the wheel in isolation")
    return found


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def _parent_site_packages() -> str:
    import site

    return site.getsitepackages()[0]


def _dsn() -> str:
    return os.environ.get("SHIORI_TEST_DATABASE_DSN", "")


def _insert(conn, session_id, content, embedding, ts, source_type="synthetic-note"):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO session_chunks
           (session_id, source_type, content, embedding, embedding_model,
            timestamp_start, timestamp_end, turn_index_start, turn_index_end,
            content_tsvector, created_at)
           VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
        (session_id, source_type, content, str(embedding), MODEL_IDENTITY,
         ts, ts, 0, 0, content, ts),
    )
    conn.commit()
    cur.close()


@pytest.fixture(scope="module")
def installed_wheel(uv: str, tmp_path_factory) -> Path:
    """Build + install the current tree as a wheel, offline, into a clean venv."""
    tmp_path = tmp_path_factory.mktemp("wheel")
    dist = tmp_path / "dist"
    dist.mkdir()
    # Build in a dedicated venv with the pyproject-pinned backend installed
    # offline from the local cache, so the build runs with
    # --no-build-isolation under a hard offline gate (no index/network).
    build_venv = tmp_path / "build-venv"
    _run(uv, "venv", "--python", str(sys.executable), str(build_venv))
    install_env = {"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    _run(uv, "pip", "install", "--offline", "--python", str(build_venv / "bin" / "python"),
         "setuptools>=75,<80", env=install_env)
    _run(uv, "build", "--offline", "--no-build-isolation",
         "--python", str(build_venv / "bin" / "python"),
         "--out-dir", str(dist), str(ROOT), cwd=ROOT)
    wheels = list(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    wheel = max(wheels, key=lambda p: p.stat().st_size)

    venv = tmp_path / "venv"
    _run(uv, "venv", "--python", str(sys.executable), str(venv))
    py = venv / "bin" / "python"
    install_env = {"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    _run(uv, "pip", "install", "--python", str(py), "--no-deps", str(wheel), env=install_env)

    bridge_env = dict(os.environ)
    bridge_env["PYTHONPATH"] = _parent_site_packages()
    out = _run(
        str(py),
        "-c",
        "import query; print(query.__file__)",
        cwd=tmp_path,
        env=bridge_env,
    )
    query_path = out.stdout.strip()
    assert str(venv / "lib") in query_path, f"query loaded from source: {query_path}"
    return venv


def test_installed_wheel_cli_preserves_distinct_evidence(
    installed_wheel: Path, db, tmp_path: Path
):
    venv = installed_wheel
    bridge_env = dict(os.environ)
    bridge_env["PYTHONPATH"] = _parent_site_packages()

    # Seed the isolated test DB with the real A/B vectors.
    conn, prefix = db
    session_id = prefix + "-wheel"
    now = datetime.now(UTC)
    from shiori.embedding_replay import ReplayEmbedder

    embedder = ReplayEmbedder.from_files(FIXTURES / "manifest.json", FIXTURES / "vectors.json")
    _insert(conn, session_id, B, embedder.embed(B, input_type="document"), now)
    _insert(conn, session_id, A, embedder.embed(A, input_type="document"), now)

    # Copy the static replay fixture into tmp and write config pointing at it.
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    for name in ("manifest.json", "corpus.jsonl", "queries.jsonl", "vectors.json"):
        shutil.copy2(FIXTURES / name, fixture_dir / name)
    config = tmp_path / "config.toml"
    config.write_text(
        "[shiori]\n"
        f'embedding_provider = "replay"\n'
        f'replay_manifest = "{fixture_dir / "manifest.json"}"\n'
        'environment = "test"\n'
        f"embed_dim = {EMBED_DIM}\n"
        f'database_dsn = "{_dsn()}"\n',
        encoding="utf-8",
    )

    cli = venv / "bin" / "shiori"
    result = _run(
        str(cli), "--config", str(config), "query", QUERY, "-n", "20",
        "--session-id", session_id, cwd=tmp_path, env=bridge_env,
    )
    assert result.returncode == 0
    b_pos = result.stdout.find(B)
    a_pos = result.stdout.find(A)
    assert b_pos != -1, f"B (current fact) missing from CLI output:\n{result.stdout}"
    assert a_pos != -1, f"A (historical fact) missing from CLI output:\n{result.stdout}"
    assert b_pos < a_pos, f"expected current-fact B before historical-fact A, got:\n{result.stdout}"
