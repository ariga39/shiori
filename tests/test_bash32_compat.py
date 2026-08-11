"""Bash 3.2 compatibility regression for the container/pgvector smoke scripts.

The repository must not rely on Bash 4-only builtins such as ``mapfile`` or
``readarray``: the runtime host (macOS) ships /bin/bash 3.2.  These tests run
the affected scripts under the system bash and cover edge cases (multi-line
output, empty collections, whitespace/special characters, subcommand failure)
to prove the Bash 3.2-compatible reading preserves ordering and fail-closed
semantics.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PGVECTOR_SCRIPT = ROOT / "tools" / "verify_pgvector_image.sh"
RUNTIME_SMOKE = ROOT / "tools" / "container_runtime_smoke.sh"
EXPECTED = "pgvector/pgvector@sha256:" + "a" * 64
CONTAINER = "b" * 64
IMAGE_ID = "c" * 64


def _system_bash() -> str:
    for candidate in ("/bin/bash", shutil.which("bash") or ""):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("no system bash found")


def _bash_major_version(bash: str) -> int:
    result = subprocess.run([bash, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def _write_fake_docker(tmp_path: Path, body: str) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            {body}
            """
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker


def _run_pgvector(tmp_path: Path, docker_body: str, bash: str) -> subprocess.CompletedProcess[str]:
    _write_fake_docker(tmp_path, docker_body)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    return subprocess.run(
        [bash, str(PGVECTOR_SCRIPT), EXPECTED, CONTAINER],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_bash4_only_array_builtins_in_smoke_scripts() -> None:
    """No unexempted mapfile/readarray in the shell tooling."""
    for script in (PGVECTOR_SCRIPT, RUNTIME_SMOKE):
        text = script.read_text(encoding="utf-8")
        assert "mapfile" not in text, f"{script.name} still uses mapfile"
        assert "readarray" not in text, f"{script.name} still uses readarray"


def test_pgvector_guard_under_bash32_multiline(tmp_path: Path) -> None:
    bash = _system_bash()
    body = """
        case "$1 $2" in
          'ps --no-trunc')
            printf '%b' 'a'$(printf '\\n')"b"$(printf '\\n') ;;

          'inspect --format') printf '%s\\n' "sha256:${IMAGE_ID}" ;;
          'image inspect') printf '%b' 'pgvector/pgvector@sha256:'$(printf '%064d' 0 | tr 0 a)'\n' ;;
          *) exit 2 ;;
        esac
    """
    result = _run_pgvector(tmp_path, body, bash)
    # multi-line output -> ambiguous container identity -> fail closed
    assert result.returncode != 0
    assert "exactly one" in result.stderr


def test_pgvector_guard_under_bash32_single(tmp_path: Path) -> None:
    bash = _system_bash()
    body = f"""
        case "$1 $2" in
          'ps --no-trunc') printf '%s\\n' '{CONTAINER}' ;;
          'inspect --format') printf '%s\\n' 'sha256:{IMAGE_ID}' ;;
          'image inspect') printf '%s\\n' '{EXPECTED}' ;;
          *) exit 2 ;;
        esac
    """
    result = _run_pgvector(tmp_path, body, bash)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == CONTAINER


def test_pgvector_guard_under_bash32_empty_output(tmp_path: Path) -> None:
    """Empty docker ps output must be an empty collection, not one blank line."""
    bash = _system_bash()
    body = """
        case "$1 $2" in
          'ps --no-trunc') printf '' ;;
          'inspect --format') printf '%s\\n' 'sha256:${IMAGE_ID}' ;;
          'image inspect') printf '%s\\n' 'pgvector/pgvector@sha256:'$(printf '%064d' 0 | tr 0 a) ;;
          *) exit 2 ;;
        esac
    """
    result = _run_pgvector(tmp_path, body, bash)
    assert result.returncode != 0
    assert "exactly one" in result.stderr


def test_pgvector_guard_under_bash32_subcommand_failure(tmp_path: Path) -> None:
    """A failing docker ps must propagate as a non-zero exit, not hang."""
    bash = _system_bash()
    body = """
        if [[ "$1 $2" == 'ps --no-trunc' ]]; then
          echo "docker daemon unreachable" >&2
          exit 1
        fi
        exit 0
    """
    result = _run_pgvector(tmp_path, body, bash)
    assert result.returncode != 0


def test_runtime_smoke_volume_scan_under_bash32(tmp_path: Path) -> None:
    """The project-volume scan uses Bash 3.2-compatible reading."""
    bash = _system_bash()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "volume-sentinel").write_text("must-survive", encoding="utf-8")
    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == compose ]]; then
              case "$*" in
                *" config --quiet") exit 0 ;;
                *" ps --quiet session-memory-pg")
                  printf '%s\\n' 'abcdef0123456789'; exit 0 ;;
                *" up --detach --force-recreate --no-deps session-memory-pg")
                  touch "{state}/up-called"; exit 0 ;;
                *" down --volumes --remove-orphans")
                  touch "{state}/down-called"; rm -f "{state}/up-called"; exit 0 ;;
                *" restart session-memory-pg") exit 0 ;;
                *" exec --no-TTY session-memory-pg id -u")
                  printf '%s\\n' '999'; exit 0 ;;
                *" exec --no-TTY session-memory-pg pg_isready")
                  exit 0 ;;
                *" exec --no-TTY --env PGPASSWORD="*" session-memory-pg psql"*)
                  case "$*" in
                    *"SHOW shared_preload_libraries;")
                      printf '%s\\n' 'vector'; exit 0 ;;
                    *"CREATE EXTENSION"*)
                      exit 0 ;;
                    *"SELECT count(*) FROM shiori_container_smoke;")
                      printf '%s\\n' '1'; exit 0 ;;
                    *"CREATE TEMP TABLE"*)
                      # Post-restart write probe: write result + new generation.
                      printf '%s\\n' '1'; printf '%s\\n' 'gen-b'; exit 0 ;;
                    *"pg_postmaster_start_time"*)
                      # Pre-restart generation identity.
                      printf '%s\\n' 'gen-a'; exit 0 ;;
                  esac
                  exit 0 ;;
                *) exit 0 ;;
              esac
            fi
            if [[ "${{1:-}}" == image && "${{2:-}}" == inspect ]]; then
              printf '%s\\n' 'sha256:{IMAGE_ID}'; exit 0
            fi
            if [[ "${{1:-}}" == inspect ]]; then
              case "$*" in
                *"Config.User"*) printf '%s\\n' 'postgres'; exit 0 ;;
                *"Config.Entrypoint"*) printf '%s\\n' '"docker-entrypoint.sh"'; exit 0 ;;
                *"Config.Cmd"*) printf '%s\\n' '["postgres","-c","shared_preload_libraries=vector"]'; exit 0 ;;
              esac
            fi
            if [[ "${{1:-}}" == volume ]]; then
              if [[ "${{2:-}}" == inspect ]]; then
                printf '%s\\n' 'project-owned'
                exit 0
              fi
              if [[ -f "{state}/up-called" ]]; then
                # single project-scoped volume created after up
                printf '%s\\n' 'smoke-project_volume'
              fi
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "synthetic",
        "POSTGRES_USER": "synthetic",
        "POSTGRES_PASSWORD": "synthetic",
        "SHIORI_PG_PORT": "55432",
    }
    result = subprocess.run(
        [bash, str(RUNTIME_SMOKE), "--project", "smoke-bash32"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "container runtime smoke passed" in result.stdout


def test_system_bash_is_available_and_used() -> None:
    bash = _system_bash()
    assert _bash_major_version(bash) >= 3
