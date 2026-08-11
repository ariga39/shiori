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
    *" ps --quiet session-memory-pg")
      printf '%s\\n' 'abcdef1234567890abcdef1234567890'
      exit 0
      ;;
    *" restart session-memory-pg")
      touch "${state}/restart-called"
      exit 0
      ;;
    *" down --volumes --remove-orphans")
      touch "${state}/down-called"
      exit 0
      ;;
    *session-memory-pg*'id -u'*)
      printf '%s\\n' '1000'
      exit 0
      ;;
    *session-memory-pg*'psql'*)
      # SHOW shared_preload_libraries -> the fake image preloads vector.
      if [[ "${args}" == *"SHOW shared_preload_libraries"* ]]; then
        printf '%s\\n' 'vector'
        exit 0
      fi
      # SELECT count(*) FROM shiori_container_smoke -> one persisted row.
      if [[ "${args}" == *"SELECT count(*) FROM shiori_container_smoke"* ]]; then
        printf '%s\\n' '1'
        exit 0
      fi
      # Restart read/write probe (CREATE TEMP TABLE + INSERT + count + identity).
      # One probe returns two lines: write result, then postmaster generation.
      if [[ "${args}" == *"CREATE TEMP TABLE"* ]]; then
        if [[ -f "${state}/restart-called" ]]; then
          count=0
          if [[ -f "${state}/probes" ]]; then
            count="$(cat "${state}/probes")"
          fi
          count=$((count + 1))
          printf '%s' "${count}" > "${state}/probes"
          # FAKE_RESTART_SQL_SHUTDOWN: the old postmaster rejects new
          # connections (shutting down) for the first two probes, then the new
          # generation accepts them. FAKE_RESTART_SQL_SHUTDOWN_FOREVER keeps
          # rejecting forever.
          if [[ "${FAKE_RESTART_SQL_SHUTDOWN:-0}" == 1 ]]; then
            if [[ "${FAKE_RESTART_SQL_SHUTDOWN_FOREVER:-0}" == 1 ]]; then
              echo "FATAL: database system is shutting down" >&2
              exit 1
            fi
            if (( count <= 2 )); then
              echo "FATAL: database system is shutting down" >&2
              exit 1
            fi
          fi
          # FAKE_RESTART_OLD_GEN: the OLD generation keeps accepting writes
          # (write probe succeeds) for the first two probes; the gate must NOT
          # pass until the generation identity differs.
          if [[ "${FAKE_RESTART_OLD_GEN:-0}" == 1 ]] && (( count <= 2 )); then
            printf '%s\\n' '1'
            printf '%s\\n' "${FAKE_OLD_GEN:-gen-a}"
            exit 0
          fi
          # FAKE_RESTART_WRITE_FAIL_FOREVER: the generation changed but the
          # write probe never succeeds.
          if [[ "${FAKE_RESTART_WRITE_FAIL_FOREVER:-0}" == 1 ]]; then
            echo "ERROR: cannot execute INSERT in a read-only transaction" >&2
            exit 1
          fi
        fi
        printf '%s\\n' '1'
        printf '%s\\n' "${FAKE_NEW_GEN:-gen-b}"
        exit 0
      fi
      # Pre-restart postmaster generation identity read.
      if [[ "${args}" == *"pg_postmaster_start_time"* ]]; then
        if [[ -f "${state}/restart-called" ]]; then
          printf '%s\\n' "${FAKE_NEW_GEN:-gen-b}"
        else
          printf '%s\\n' "${FAKE_OLD_GEN:-gen-a}"
        fi
        exit 0
      fi
      exit 0
      ;;
    *)
      exit 0
      ;;
  esac
fi

if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  exit 0
fi

if [[ "${1:-}" == inspect ]]; then
  # ${@: -1} is the container id; the format selector determines the output.
  case "${args}" in
    *"{{.Config.User}}"*)
      printf '%s\\n' 'postgres'
      exit 0
      ;;
    *"{{json .Config.Entrypoint}}"*)
      printf '%s\\n' '["/docker-entrypoint.sh"]'
      exit 0
      ;;
    *"{{json .Config.Cmd}}"*)
      printf '%s\\n' '["postgres","-c","shared_preload_libraries=vector"]'
      exit 0
      ;;
    *)
      exit 0
      ;;
  esac
fi

if [[ "${1:-}" == volume && "${2:-}" == inspect ]]; then
  printf '%s\\n' 'project-owned'
  exit 0
fi

if [[ "${1:-}" == volume && "${2:-}" == ls ]]; then
  # After `up`, compose created exactly one project-scoped volume.
  # After down (cleanup) compose removed the volume; before down, it exists.
  if [[ -f "${state}/down-called" ]]; then
    exit 0
  fi
  if [[ -f "${state}/up-called" ]]; then
    printf '%s\\n' 'shiori_volume_sentinel'
  else
    [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\\n' 'volume-sentinel'
  fi
  exit 0
fi

if [[ "${1:-}" == ps ]]; then
  if [[ -f "${state}/down-called" ]]; then
    exit 0
  fi
  [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\\n' 'container-sentinel'
  exit 0
fi
if [[ "${1:-}" == network && "${2:-}" == ls ]]; then
  if [[ -f "${state}/down-called" ]]; then
    exit 0
  fi
  [[ "${FAKE_EXISTING:-0}" == 1 ]] && printf '%s\\n' 'network-sentinel'
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
        "SHIORI_PG_PORT": "55432",
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


def test_restart_readiness_waits_for_stable_sql_probe(tmp_path: Path) -> None:
    """pg_isready can succeed while the old postmaster is still shutting down.
    With FAKE_RESTART_SQL_SHUTDOWN=1, the first two post-restart write probes
    fail (simulating 'database system is shutting down'); the script must wait
    until the NEW generation accepts a real write probe instead of passing on
    the stale pg_isready signal."""
    result, state = _run(tmp_path, FAKE_RESTART_SQL_SHUTDOWN="1", SHIORI_SMOKE_MAX_ATTEMPTS="5")

    assert result.returncode == 0, result.stderr
    assert (state / "restart-called").exists()
    # The script must have retried the write probe past the simulated
    # shutting-down window (>= 3 probes: 2 failures + the stable new-generation
    # successes).
    probes = int((state / "probes").read_text(encoding="utf-8"))
    assert probes >= 3


def test_restart_readiness_fails_closed_if_sql_never_stabilizes(tmp_path: Path) -> None:
    """If the post-restart write probe never stabilizes (shutting down persists),
    the script must fail closed rather than pass."""
    result, state = _run(
        tmp_path,
        FAKE_RESTART_SQL_SHUTDOWN="1",
        FAKE_RESTART_SQL_SHUTDOWN_FOREVER="1",
        SHIORI_SMOKE_MAX_ATTEMPTS="5",
    )

    assert result.returncode != 0
    assert "did not become read/write ready after restart" in result.stderr
    assert int((state / "probes").read_text(encoding="utf-8")) == 5


def test_old_generation_success_does_not_advance_readiness(tmp_path: Path) -> None:
    """A write probe that SUCCEEDS on the OLD postmaster generation must never
    advance the gate: only a probe on a NEW generation (identity changed)
    counts, so even consecutive old-generation write successes are ignored."""
    result, state = _run(tmp_path, FAKE_RESTART_OLD_GEN="1", SHIORI_SMOKE_MAX_ATTEMPTS="5")

    assert result.returncode == 0, result.stderr
    # Probes 1-2 succeeded as writes but reported the OLD generation; the gate
    # must not have passed on them. It only opened after the identity changed
    # and stayed stable across the poll interval (>= 3 probes).
    probes = int((state / "probes").read_text(encoding="utf-8"))
    assert probes >= 3


def test_new_generation_write_probe_failure_fails_closed(tmp_path: Path) -> None:
    """If the generation changes but the real write probe persistently fails,
    the script must fail closed instead of passing on the identity alone."""
    result, state = _run(
        tmp_path,
        FAKE_RESTART_WRITE_FAIL_FOREVER="1",
        SHIORI_SMOKE_MAX_ATTEMPTS="5",
    )

    assert result.returncode != 0
    assert "did not become read/write ready after restart" in result.stderr
    # Every polling attempt issued a probe; all failed to pass the gate.
    assert int((state / "probes").read_text(encoding="utf-8")) == 5
