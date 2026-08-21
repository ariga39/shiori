"""Consolidated wheel-packaging smoke (issue #27).

Builds the current tree into a wheel once per session, installs it into a
clean venv without network or model access, then proves — through one
parametrized test — that the installed public surface survives packaging:

- the ``shiori`` CLI entry point exists and exposes every command group,
- the installed MCP module carries the expected tool parameters,
- the installed package imports from the wheel's site-packages only (no
  source tree, no network, no model access).

Runtime dependencies are bridged from the running interpreter's
site-packages via a controlled PYTHONPATH (never repo root/cwd); the wheel
is installed ``--no-deps`` under a hard offline gate.  Venv layout is
platform-appropriate (``Scripts`` on Windows, ``bin`` elsewhere).
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CLI_COMMAND_GROUPS = ("ingest", "query", "serve", "db", "privacy")
QUERY_FILTER_FLAGS = ("--source-type", "--session-id", "--time-from", "--time-to")
MCP_TOOL_PARAMS = ("query", "limit", "offset", "source_types", "session_ids", "time_from", "time_to")
MCP_RESPONSE_KEYS = ("filters_applied",)

SURFACES = (
    "cli-entry-point",
    "cli-command-groups",
    "cli-query-filter-flags",
    "mcp-tool-parameters",
    "mcp-response-keys",
    "offline-imports",
)


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv: Path) -> Path:
    return _venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


def _venv_shiori(venv: Path) -> Path:
    return _venv_bin(venv) / ("shiori.exe" if os.name == "nt" else "shiori")


def _python_site_packages(python: str) -> Path:
    """The interpreter's actual site-packages directory.

    ``site.getsitepackages()[0]`` is the venv root on Windows (the
    ``Lib\\site-packages`` entry comes second), so pick the first
    ``site-packages`` entry, falling back to the last entry."""
    code = (
        "import site;"
        "paths = [p for p in site.getsitepackages() if 'site-packages' in p];"
        "print((paths or site.getsitepackages())[-1])"
    )
    out = _run(python, "-c", code)
    return Path(out.stdout.strip())


def _bridge_env() -> dict[str, str]:
    """Parent interpreter site-packages, bridging runtime deps (numpy/
    psycopg2/mcp) into the child wheel venv.  query.py/mcp_server.py are not
    physically present there (only via the parent's editable .pth, which a
    plain PYTHONPATH dir does not execute), so they resolve from the wheel.

    On Windows, pywin32's own .pth entries (win32, win32\\lib, pythonwin)
    are likewise not processed from a PYTHONPATH dir; they are appended
    explicitly when present so mcp's Windows utilities stay importable."""
    parent_site_packages = _python_site_packages(sys.executable)
    parts = [str(parent_site_packages)]
    for sub in ("win32", os.path.join("win32", "lib"), "pythonwin"):
        candidate = parent_site_packages / sub
        if candidate.is_dir():
            parts.append(str(candidate))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


@pytest.fixture(scope="session")
def uv() -> str:
    found = shutil.which("uv")
    if not found:
        pytest.skip("uv is required to build/install the wheel in isolation")
    return found


@pytest.fixture(scope="session")
def installed_wheel(uv: str, tmp_path_factory) -> Path:
    """Build + install the current tree as a wheel, offline, into a clean venv.

    The wheel is built with the locked project build environment (the current
    interpreter populated by ``uv sync --locked --extra dev``) using
    ``--no-build-isolation`` under a hard offline gate; the clean venv only
    installs the resulting wheel ``--no-deps`` with UV_OFFLINE/PIP_NO_INDEX
    set.  No dedicated build venv is created."""
    tmp_path = tmp_path_factory.mktemp("wheel-smoke")
    dist = tmp_path / "dist"
    dist.mkdir()
    _run(
        uv, "build", "--offline", "--no-build-isolation",
        "--python", str(sys.executable),
        "--out-dir", str(dist), str(ROOT), cwd=ROOT,
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    wheel = max(wheels, key=lambda p: p.stat().st_size)

    venv = tmp_path / "venv"
    _run(uv, "venv", "--python", str(sys.executable), str(venv))
    # Derive from os.environ: replacing the environment wholesale would drop
    # TEMP/SYSTEMROOT on Windows and break uv's temp-file handling.
    install_env = dict(os.environ)
    install_env["UV_OFFLINE"] = "1"
    install_env["PIP_NO_INDEX"] = "1"
    _run(
        uv, "pip", "install",
        "--python", str(_venv_python(venv)),
        "--no-deps", str(wheel),
        env=install_env,
    )
    return venv


@pytest.fixture(scope="session")
def wheel_site_packages(installed_wheel: Path) -> Path:
    return _python_site_packages(str(_venv_python(installed_wheel)))


def _probe_cli_entry_point(installed_wheel: Path) -> None:
    cli = _venv_shiori(installed_wheel)
    assert cli.exists(), f"installed wheel lacks CLI entry point {cli}"
    result = _run(str(cli), "--help")
    assert "usage:" in result.stdout


def _probe_cli_command_groups(installed_wheel: Path) -> None:
    cli = _venv_shiori(installed_wheel)
    top_help = _run(str(cli), "--help").stdout
    for command in CLI_COMMAND_GROUPS:
        assert command in top_help, f"{command} missing from installed `shiori --help`"
        group_help = _run(str(cli), command, "--help").stdout
        assert "usage:" in group_help, f"`shiori {command} --help` produced no usage"


def _probe_cli_query_filter_flags(installed_wheel: Path) -> None:
    cli = _venv_shiori(installed_wheel)
    result = _run(str(cli), "query", "--help")
    for flag in QUERY_FILTER_FLAGS:
        assert flag in result.stdout, f"{flag} missing from installed `shiori query --help`"


def _installed_module_functions(site_packages: Path, module: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    module_path = site_packages / f"{module}.py"
    assert module_path.is_file(), f"installed wheel lacks {module_path}"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _function_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [a.arg for a in (*node.args.args, *node.args.kwonlyargs)]


def _probe_mcp_tool_parameters(wheel_site_packages: Path) -> None:
    functions = _installed_module_functions(wheel_site_packages, "mcp_server")
    search_tools = [functions[name] for name in ("_search_tool", "run_search") if name in functions]
    assert search_tools, "installed mcp_server lacks a search tool function"
    for param in MCP_TOOL_PARAMS:
        assert any(param in _function_params(fn) for fn in search_tools), (
            f"{param} missing from installed MCP search tool"
        )


def _probe_mcp_response_keys(wheel_site_packages: Path) -> None:
    source = (wheel_site_packages / "mcp_server.py").read_text(encoding="utf-8")
    for key in MCP_RESPONSE_KEYS:
        assert key in source, f"{key} missing from installed mcp_server"


def _probe_offline_imports(installed_wheel: Path, wheel_site_packages: Path) -> None:
    # Run from an unrelated cwd so sys.path[0] is neutral; the bridge carries
    # runtime deps only, so query/mcp_server/shiori must resolve from the
    # wheel's site-packages.
    out = _run(
        str(_venv_python(installed_wheel)),
        "-c",
        (
            "import query, mcp_server, shiori.cli;"
            "print(query.__file__); print(mcp_server.__file__);"
            "print(shiori.cli.__file__)"
        ),
        cwd=installed_wheel.parent,
        env=_bridge_env(),
    )
    for loaded in out.stdout.strip().splitlines():
        assert str(wheel_site_packages) in loaded, f"module loaded outside the wheel: {loaded}"


_PROBES = {
    "cli-entry-point": lambda installed_wheel, site_pkgs: _probe_cli_entry_point(installed_wheel),
    "cli-command-groups": lambda installed_wheel, site_pkgs: _probe_cli_command_groups(installed_wheel),
    "cli-query-filter-flags": lambda installed_wheel, site_pkgs: _probe_cli_query_filter_flags(installed_wheel),
    "mcp-tool-parameters": lambda installed_wheel, site_pkgs: _probe_mcp_tool_parameters(site_pkgs),
    "mcp-response-keys": lambda installed_wheel, site_pkgs: _probe_mcp_response_keys(site_pkgs),
    "offline-imports": lambda installed_wheel, site_pkgs: _probe_offline_imports(installed_wheel, site_pkgs),
}


@pytest.mark.parametrize("surface", SURFACES)
def test_installed_wheel_packaging_surface(surface: str, installed_wheel: Path, wheel_site_packages: Path) -> None:
    """The installed wheel's public surface works offline, one probe per surface."""
    _PROBES[surface](installed_wheel, wheel_site_packages)
