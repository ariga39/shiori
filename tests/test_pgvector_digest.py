from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "verify_pgvector_image.sh"
EXPECTED = "pgvector/pgvector@sha256:" + "a" * 64
CONTAINER = "b" * 64
IMAGE_ID = "c" * 64


def _run_guard(tmp_path: Path, repo_digests: list[str], *, containers: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    docker = tmp_path / "docker"
    listed = containers if containers is not None else [CONTAINER]
    digest_lines = "".join(f"{value}\n" for value in repo_digests)
    container_lines = "".join(f"{value}\n" for value in listed)
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case \"$1 $2\" in\n"
        f"  'ps --no-trunc') printf '%b' {container_lines!r} ;;\n"
        f"  'inspect --format') printf '%s\\n' 'sha256:{IMAGE_ID}' ;;\n"
        f"  'image inspect') printf '%b' {digest_lines!r} ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    return subprocess.run(
        [str(SCRIPT), EXPECTED, CONTAINER],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pgvector_digest_guard_accepts_only_exact_image_digest(tmp_path: Path) -> None:
    passed = _run_guard(tmp_path, [EXPECTED])
    assert passed.returncode == 0
    assert passed.stdout.strip() == CONTAINER


def test_pgvector_digest_guard_rejects_empty_or_mismatched_repo_digests(tmp_path: Path) -> None:
    for repo_digests in ([], ["pgvector/pgvector@sha256:" + "d" * 64]):
        failed = _run_guard(tmp_path, repo_digests)
        assert failed.returncode != 0
        assert "pinned image" in failed.stderr


def test_pgvector_digest_guard_rejects_ambiguous_service_identity(tmp_path: Path) -> None:
    failed = _run_guard(tmp_path, [EXPECTED], containers=[CONTAINER, "d" * 64])
    assert failed.returncode != 0
    assert "exactly one" in failed.stderr
