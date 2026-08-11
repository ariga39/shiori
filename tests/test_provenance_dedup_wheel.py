"""Task #33 — installed-wheel CLI public characterization for provenance dedup.

Builds the current tree into a wheel offline, installs it into a clean venv
without network/model access, then verifies the installed CLI ``query``
preserves distinct-evidence ordering (current fact B before historical fact A)
through the real PostgreSQL seam.  No source-tree module is loaded by the child;
``query`` must resolve from the wheel's site-packages only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
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


def test_wheel_build_backend_is_locked_for_offline_ci():
    """The offline wheel harness needs the setuptools build backend available
    from the shared uv cache.  This is a packaging contract: the frozen
    `[build-system].requires` must also appear in the locked `dev` extra, and
    `uv.lock` must contain a single setuptools package in `>=75,<80` with
    verifiable artifact hashes.  Expected values are frozen config literals;
    no resolver runs and no private helper is tested."""
    with (ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)

    assert pyproject["build-system"]["requires"] == ["setuptools>=75,<80"]
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert "setuptools>=75,<80" in dev

    with (ROOT / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    setuptools = [p for p in lock["package"] if p.get("name") == "setuptools"]
    assert len(setuptools) == 1, f"expected exactly one setuptools package, got {len(setuptools)}"
    pkg = setuptools[0]
    assert "75.0.0" <= pkg["version"] < "80.0.0", f"setuptools version out of range: {pkg['version']}"
    assert "sdist" in pkg and "hash" in pkg["sdist"], "setuptools lock entry must have a verifiable sdist hash"


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


def _seed_distinct_evidence(db, session_id: str) -> None:
    """Seed the isolated test DB with the real A/B vectors under one session."""
    conn, _ = db
    now = datetime.now(UTC)
    from shiori.embedding_replay import ReplayEmbedder

    embedder = ReplayEmbedder.from_files(FIXTURES / "manifest.json", FIXTURES / "vectors.json")
    _insert(conn, session_id, B, embedder.embed(B, input_type="document"), now)
    _insert(conn, session_id, A, embedder.embed(A, input_type="document"), now)


def _write_config(tmp_path: Path) -> Path:
    """Copy the static replay fixture into tmp and write config pointing at it."""
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
    return config


def test_installed_wheel_cli_preserves_distinct_evidence(
    installed_wheel: Path, db, tmp_path: Path
):
    venv = installed_wheel
    bridge_env = dict(os.environ)
    bridge_env["PYTHONPATH"] = _parent_site_packages()

    conn, prefix = db
    session_id = prefix + "-wheel"
    _seed_distinct_evidence(db, session_id)
    config = _write_config(tmp_path)

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


def test_installed_wheel_mcp_preserves_distinct_evidence(
    installed_wheel: Path, db, tmp_path: Path
):
    venv = installed_wheel
    bridge_env = dict(os.environ)
    bridge_env["PYTHONPATH"] = _parent_site_packages()

    # The child mcp_server must resolve from the wheel's site-packages, not the
    # source tree, when run from an unrelated cwd.
    out = _run(
        str(venv / "bin" / "python"),
        "-c",
        "import mcp_server; print(mcp_server.__file__)",
        cwd=tmp_path,
        env=bridge_env,
    )
    mcp_path = out.stdout.strip()
    assert str(venv / "lib") in mcp_path, f"mcp_server loaded from source: {mcp_path}"

    conn, prefix = db
    session_id = prefix + "-wheel-mcp"
    _seed_distinct_evidence(db, session_id)
    config = _write_config(tmp_path)
    cli = venv / "bin" / "shiori"

    probe = _MCP_PROBE.replace("__QUERY__", QUERY).replace("__SID__", session_id)
    result = _run(
        str(venv / "bin" / "python"), "-c", probe, str(cli), str(config),
        cwd=tmp_path, env=bridge_env,
    )
    assert result.returncode == 0, f"MCP probe failed:\n{result.stderr}\n{result.stdout}"
    assert "MCP_DISTINCT_OK" in result.stdout


_MCP_PROBE = r'''
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

cli, config = sys.argv[1], sys.argv[2]
env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"
server = StdioServerParameters(command=cli, args=["--config", config, "serve"], env=env, cwd=str(Path(config).parent))

QUERY = "__QUERY__"
SID = "__SID__"
EXPECTED = [(QUERY_CONTENT_B, SID, "synthetic-note"), (QUERY_CONTENT_A, SID, "synthetic-note")]


async def main():
    args = {"query": QUERY, "limit": 20, "offset": 0, "session_ids": [SID]}
    async with stdio_client(server) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search", args)
            if res.isError or not res.content:
                return
            payload = json.loads(res.content[0].text)
    rows = [(row["content"], row["session_id"], row["source_type"]) for row in payload.get("results", [])]
    if rows == EXPECTED:
        print("MCP_DISTINCT_OK")


asyncio.run(main())
'''.replace("QUERY_CONTENT_B", json.dumps(B)).replace("QUERY_CONTENT_A", json.dumps(A))
