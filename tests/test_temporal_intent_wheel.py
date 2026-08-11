"""Phase 4E2 — installed-wheel public characterization (search_page/CLI/MCP).

Builds the actual wheel, installs it into a clean virtualenv WITHOUT network
or model access, then verifies the installed `query`/`mcp_server` modules load
from the wheel (not the source tree) and that ordinary/`latest`/structured-time
queries produce the same intent-gated ordering through the installed CLI and
the read-only MCP `search` tool.  No task #10 harness/fixtures or
`tools/mcp_stdio_smoke.py` are modified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from tests.test_temporal_intent import (  # noqa: E402
    DIM,
    FAR_EMB,
    LATEST_QUERY_TEXT,
    NEW_CONTENT,
    OLD_CONTENT,
    QUERY_EMB,
    QUERY_TEXT,
    _insert,
    _write_fixture,
)

# A structured-time query reuses the in-manifest QUERY_TEXT (replay->QUERY_EMB).
TIME_BOUNDS_QUERY_TEXT = QUERY_TEXT

SESSION_PREFIX = "phase4e2-wheel"


@pytest.fixture(scope="module")
def uv() -> str:
    found = shutil.which("uv")
    if not found:
        pytest.skip("uv is required to build/install the wheel in isolation")
    assert found is not None
    return found


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def _parent_site_packages() -> str:
    """The running interpreter's site-packages, used only to bridge runtime
    deps (numpy/psycopg2/mcp) into the child wheel venv.  query.py/mcp_server.py
    are NOT physically present there (only via the parent's editable .pth,
    which a plain PYTHONPATH dir does not execute), so they resolve from the
    wheel."""
    import site

    return site.getsitepackages()[0]


def _seed_db(conn, sid: str) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    _insert(conn, sid, OLD_CONTENT, QUERY_EMB, old)
    _insert(conn, sid, NEW_CONTENT, FAR_EMB, now)


def test_installed_wheel_cli_and_mcp_intent_ordering(uv: str, tmp_path: Path, db):
    """The installed wheel's CLI `query` and MCP `search` must reproduce the
    intent-gated ordering (ordinary: old-first; `latest`/structured-bounds:
    new-first) and load from the wheel, not the source tree.

    The local wheel is installed --no-deps with UV_OFFLINE/PIP_NO_INDEX set
    (no index access).  Runtime deps are bridged from the running interpreter's
    site-packages via a controlled PYTHONPATH (never repo root/cwd; the parent's
    editable .pth is not processed from a plain PYTHONPATH dir), so
    query.py/mcp_server.py load from the child wheel site-packages."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _run(uv, "build", "--out-dir", str(dist), str(ROOT), cwd=ROOT)
    wheels = list(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    wheel = max(wheels, key=lambda p: p.stat().st_size)

    # Clean venv + the wheel --no-deps under a hard offline/no-index gate.
    venv = tmp_path / "venv"
    _run(uv, "venv", "--python", str(sys.executable), str(venv))
    py = venv / "bin" / "python"
    install_env = {"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    _run(uv, "pip", "install", "--python", str(py), "--no-deps", str(wheel), env=install_env)

    # Offline gate evidence: a second offline install attempt must succeed only
    # from cache/index-free resolution (the wheel is already present).
    _run(uv, "pip", "install", "--python", str(py), "--no-deps", str(wheel), env=install_env)

    # Verify the installed modules resolve from the wheel site-packages, not
    # the source tree (run from an unrelated cwd so sys.path[0] is neutral).
    # PYTHONPATH bridges runtime deps from the parent interpreter.
    bridge_env = dict(os.environ)
    bridge_env["PYTHONPATH"] = _parent_site_packages()
    out = _run(
        str(py),
        "-c",
        "import query, mcp_server, numpy, psycopg2; print(query.__file__); print(mcp_server.__file__); print(numpy.__file__)",
        cwd=tmp_path,
        env=bridge_env,
    )
    lines = out.stdout.strip().splitlines()
    assert len(lines) == 3, f"expected 3 path lines, got {lines}"
    query_path, mcp_path, numpy_path = lines
    assert str(venv / "lib") in query_path, f"query loaded from source: {query_path}"
    assert str(venv / "lib") in mcp_path, f"mcp_server loaded from source: {mcp_path}"
    # numpy resolves from the parent interpreter site-packages (the bridge), not
    # from the wheel (which was installed --no-deps).
    assert _parent_site_packages() in numpy_path

    # Seed the isolated test DB with the two rows under the guarded fixture.
    conn, prefix = db
    sid = prefix + "-wheel"
    _seed_db(conn, sid)

    # Write the replay manifest (tmp_path) + config.toml pointing at it.
    manifest_dir = tmp_path / "fixture"
    manifest_dir.mkdir()
    manifest = _write_fixture(manifest_dir)
    config = tmp_path / "config.toml"
    config.write_text(
        "[shiori]\n"
        f'embedding_provider = "replay"\n'
        f'replay_manifest = "{manifest}"\n'
        'environment = "test"\n'
        f"embed_dim = {DIM}\n"
        f'database_dsn = "{_dsn()}"\n',
        encoding="utf-8",
    )

    cli = venv / "bin" / "shiori"

    # Ordinary query: no decay -> old relevant first.
    ordinary = _run(
        str(cli), "--config", str(config), "query", QUERY_TEXT, "-n", "20",
        "--session-id", sid, cwd=tmp_path, env=bridge_env,
    )
    assert ordinary.returncode == 0
    old_pos = ordinary.stdout.find(OLD_CONTENT)
    new_pos = ordinary.stdout.find(NEW_CONTENT)
    assert old_pos != -1 and new_pos != -1
    assert old_pos < new_pos, f"ordinary: expected old-first, got\n{ordinary.stdout}"

    # `latest`: decay applies -> new weak first.
    latest = _run(
        str(cli), "--config", str(config), "query", LATEST_QUERY_TEXT, "-n", "20",
        "--session-id", sid, cwd=tmp_path, env=bridge_env,
    )
    old_pos = latest.stdout.find(OLD_CONTENT)
    new_pos = latest.stdout.find(NEW_CONTENT)
    assert old_pos != -1 and new_pos != -1
    assert new_pos < old_pos, f"latest: expected new-first, got\n{latest.stdout}"

    # Structured time bounds: decay applies -> new weak first.  The lower
    # bound is before the OLD row so both rows survive the filter and the
    # intent-gated decay ordering is observable.
    time_filtered = _run(
        str(cli),
        "--config", str(config),
        "query", TIME_BOUNDS_QUERY_TEXT,
        "-n", "20",
        "--session-id", sid,
        "--time-from", (datetime.now(UTC) - timedelta(days=121)).isoformat(),
        cwd=tmp_path,
        env=bridge_env,
    )
    old_pos = time_filtered.stdout.find(OLD_CONTENT)
    new_pos = time_filtered.stdout.find(NEW_CONTENT)
    assert old_pos != -1 and new_pos != -1, f"missing row in structured query\n{time_filtered.stdout}"
    assert new_pos < old_pos, f"structured: expected new-first, got\n{time_filtered.stdout}"

    # MCP stdio: the installed `shiori ... serve` exposes the read-only `search`
    # tool; ordinary/`latest`/structured-time ordering, filters_applied, and
    # provenance must be consistent with the CLI/real-PG.
    mcp_script = _run(
        str(py), "-c", _MCP_PROBE, str(cli), str(config), sid,
        cwd=tmp_path, env=bridge_env,
    )
    assert mcp_script.returncode == 0, f"MCP probe failed:\n{mcp_script.stderr}"
    assert "MCP_ORDINARY_OK" in mcp_script.stdout
    assert "MCP_LATEST_OK" in mcp_script.stdout
    assert "MCP_STRUCTURED_OK" in mcp_script.stdout


_MCP_PROBE = r'''
import asyncio, json, os, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

cli, config, sid = sys.argv[1], sys.argv[2], sys.argv[3]
env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"
server = StdioServerParameters(command=cli, args=["--config", config, "serve"], env=env, cwd=str(Path(config).parent))

EXPECTED_MODEL = "voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f"

def _check_provenance(row, sid):
    ok = (
        row.get("session_id") == sid
        and row.get("source_type") == "main_user"
        and row.get("embedding_model") == EXPECTED_MODEL
        and row.get("embedding_dimension") == 1024
    )
    prov = row.get("provenance", {})
    return ok and prov.get("session_id") == sid and prov.get("source_type") == "main_user" \
        and prov.get("embedding_model") == EXPECTED_MODEL and prov.get("embedding_dimension") == 1024

async def _order(text, time_from=None):
    args = {"query": text, "limit": 20, "offset": 0, "session_ids": [sid]}
    if time_from is not None:
        args["time_from"] = time_from
    async with stdio_client(server) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search", args)
            if res.isError or not res.content:
                return None
            payload = json.loads(res.content[0].text)
            return payload

async def main():
    now = datetime.now(UTC)
    covering = (now - timedelta(days=121)).isoformat()

    ordinary = await _order("__QT__")
    latest = await _order("__LT__")
    structured = await _order("__QT__", time_from=covering)

    if ordinary and ordinary.get("filters_applied", {}).get("session_ids") is True:
        if ordinary.get("filters_applied", {}).get("time_from") is None:
            if ordinary.get("filters_applied", {}).get("time_to") is None:
                rows = ordinary.get("results", [])
                if len(rows) >= 2 and all(_check_provenance(r, sid) for r in rows):
                    contents = [r["content"] for r in rows]
                    if contents.index("__OC__") < contents.index("__NC__"):
                        print("MCP_ORDINARY_OK")

    if latest and latest.get("filters_applied", {}).get("session_ids") is True:
        if latest.get("filters_applied", {}).get("time_from") is None:
            if latest.get("filters_applied", {}).get("time_to") is None:
                rows = latest.get("results", [])
                if len(rows) >= 2 and all(_check_provenance(r, sid) for r in rows):
                    contents = [r["content"] for r in rows]
                    if contents.index("__NC__") < contents.index("__OC__"):
                        print("MCP_LATEST_OK")

    if structured and structured.get("filters_applied", {}).get("session_ids") is True:
        if structured.get("filters_applied", {}).get("time_from"):
            rows = structured.get("results", [])
            if len(rows) >= 2 and all(_check_provenance(r, sid) for r in rows):
                contents = [r["content"] for r in rows]
                if contents.index("__NC__") < contents.index("__OC__"):
                    print("MCP_STRUCTURED_OK")

asyncio.run(main())
'''.replace("__QT__", QUERY_TEXT).replace("__LT__", LATEST_QUERY_TEXT).replace(
    "__OC__", OLD_CONTENT
).replace("__NC__", NEW_CONTENT)


def _dsn() -> str:
    import os

    return os.environ.get("SHIORI_TEST_DATABASE_DSN", "")
