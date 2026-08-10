from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tools" / "container_runtime_smoke.sh"


def _fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
state="${FAKE_STATE}"
args="$*"

if [[ "${1:-}" == compose ]]; then
  case "${args}" in
    *" config --quiet")
      [[ "${FAKE_CONFIG_FAIL:-0}" != 1 ]] || exit 9
      exit 0
      ;;
    *" build --pull session-memory-pg")
      [[ "${FAKE_BUILD_FAIL:-0}" != 1 ]] || exit 10
      exit 0
      ;;
    *" up --detach --force-recreate --no-deps session-memory-pg")
      touch "${state}/up-called"
      [[ "${FAKE_UP_FAIL:-0}" != 1 ]] || exit 11
      exit 0
      ;;
    *" down --volumes --remove-orphans")
      touch "${state}/down-called"
      exit 0
      ;;
    *)
      exit 0
      ;;
  esac
fi

if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  exit 0
fi

if [[ "${1:-}" == ps ]]; then
  [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\n' 'container-sentinel'
  exit 0
fi
if [[ "${1:-}" == network ]]; then
  [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\n' 'network-sentinel'
  exit 0
fi
if [[ "${1:-}" == volume ]]; then
  [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\n' 'volume-sentinel'
  exit 0
fi

exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, **flags: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    state = tmp_path / "state"
    state.mkdir()
    sentinel = state / "volume-sentinel"
    sentinel.write_text("must-survive", encoding="utf-8")
    bin_dir = _fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "POSTGRES_DB": "synthetic",
        "POSTGRES_USER": "synthetic",
        "POSTGRES_PASSWORD": "synthetic",
        "SHIYI_PG_PORT": "55432",
        **flags,
    }
    result = subprocess.run(
        [str(SCRIPT), "--project", "smoke-safety"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, state


def test_existing_project_resources_survive_preflight_failure(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_EXISTING="1")

    assert result.returncode != 0
    assert "refusing to reuse existing resources" in result.stderr
    assert not (state / "down-called").exists()
    assert (state / "volume-sentinel").read_text(encoding="utf-8") == "must-survive"


def test_config_failure_has_no_destructive_cleanup_trap(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_CONFIG_FAIL="1")

    assert result.returncode != 0
    assert not (state / "down-called").exists()


def test_build_failure_has_no_destructive_cleanup_trap(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_BUILD_FAIL="1")

    assert result.returncode != 0
    assert not (state / "down-called").exists()


def test_up_failure_cleans_resources_after_ownership_gate(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_UP_FAIL="1")

    assert result.returncode != 0
    assert (state / "up-called").exists()
    assert (state / "down-called").exists()
