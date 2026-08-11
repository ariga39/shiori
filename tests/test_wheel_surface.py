"""Wheel-only public-surface proof (Phase 4E1).

Builds the actual wheel in an isolated directory, installs it into a clean
virtualenv WITHOUT network or model access, then proves the installed CLI entry
point exposes the new filter flags and the installed MCP module carries the new
filter parameters.  This guards against source-tree-only coverage: the public
surface must survive packaging.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

WHEEL_FILTER_FLAGS = ("--source-type", "--session-id", "--time-from", "--time-to")
MCP_FILTER_PARAMS = ("source_types", "session_ids", "time_from", "time_to")
MCP_FILTER_KEYS = ("filters_applied", "source_types", "session_ids", "time_from", "time_to")


@pytest.fixture(scope="module")
def uv() -> str:
    found = shutil.which("uv")
    if not found:
        pytest.skip("uv is required to build/install the wheel in isolation")
    assert found is not None
    return found


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def _installed_site_packages(venv: Path) -> Path:
    py = venv / "bin" / "python"
    out = subprocess.run(
        [str(py), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def test_wheel_only_surface_carries_cli_and_mcp_filter_public_surface(uv: str, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _run(uv, "build", "--out-dir", str(dist), str(ROOT), cwd=ROOT)
    wheels = list(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    wheel = max(wheels, key=lambda p: p.stat().st_size)

    venv = tmp_path / "venv"
    _run(uv, "venv", "--python", "3.12", str(venv))
    py = venv / "bin" / "python"
    # --no-deps keeps the check fully offline: nothing beyond the wheel itself.
    _run(uv, "pip", "install", "--python", str(py), "--no-deps", str(wheel))

    # 1. CLI entry point exposes every filter flag via --help.
    cli = venv / "bin" / "shiori"
    help_result = _run(str(cli), "query", "--help")
    for flag in WHEEL_FILTER_FLAGS:
        assert flag in help_result.stdout, f"{flag} missing from installed `shiori query --help`"

    # 2. The installed MCP module carries the filter surface.
    site_packages = _installed_site_packages(venv)
    mcp_path = site_packages / "mcp_server.py"
    assert mcp_path.is_file(), f"installed wheel lacks {mcp_path}"
    tree = ast.parse(mcp_path.read_text(encoding="utf-8"))

    search_tool_args: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_search_tool", "run_search"):
            search_tool_args.extend(a.arg for a in node.args.args)
            search_tool_args.extend(a.arg for a in node.args.kwonlyargs)
    for param in MCP_FILTER_PARAMS:
        assert param in search_tool_args, f"{param} missing from installed MCP module"

    source = mcp_path.read_text(encoding="utf-8")
    assert "filters_applied" in source

    # The stable error codes originate in the installed query module (the MCP
    # boundary maps them one-for-one), so prove the wheel carries both halves.
    query_path = site_packages / "query.py"
    assert query_path.is_file(), f"installed wheel lacks {query_path}"
    query_source = query_path.read_text(encoding="utf-8")
    for code in ("invalid_filter_type", "invalid_filter_value", "filter_count_exceeded"):
        assert code in query_source, f"{code} missing from installed query module"
