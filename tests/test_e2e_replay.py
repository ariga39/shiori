"""Phase 4B fixture-backed full E2E test (skipped without isolated PostgreSQL).

Runs tools/e2e_replay_smoke.sh against the opt-in isolated test database with
a wheel-built CLI, exercising the real installed surface end to end with the
versioned replay-embedding fixture.  No model, network, credential, or
prebuilt database is involved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E_SCRIPT = ROOT / "tools" / "e2e_replay_smoke.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "replay"

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("SHIORI_TEST_DATABASE_DSN")
        and os.environ.get("SHIORI_TEST_DATABASE_NAME")
        and os.environ.get("SHIORI_TEST_DATABASE_MARKER")
    ),
    reason="isolated PostgreSQL not configured",
)


def test_e2e_replay_smoke(tmp_path: Path) -> None:
    cli = os.environ.get("SHIORI_TEST_CLI")
    python_bin = os.environ.get("SHIORI_TEST_PYTHON")
    if not cli or not python_bin:
        pytest.skip("SHIORI_TEST_CLI/SHIORI_TEST_PYTHON not set (wheel install required)")
    if not Path(cli).is_file() or not Path(python_bin).is_file():
        pytest.skip("declared test CLI/Python not present")

    result = subprocess.run(
        [
            str(E2E_SCRIPT),
            "--cli", cli,
            "--python", python_bin,
            "--dsn", os.environ["SHIORI_TEST_DATABASE_DSN"],
            "--database-name", os.environ["SHIORI_TEST_DATABASE_NAME"],
            "--workdir", str(tmp_path / "harness"),
            "--fixture-dir", str(FIXTURES),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SHIORI_TEST_DATABASE_MARKER": os.environ["SHIORI_TEST_DATABASE_MARKER"],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"e2e replay smoke failed:\n{result.stdout}\n{result.stderr}"
    assert "e2e replay smoke ok" in result.stdout


def test_replay_fixture_committed_files_present() -> None:
    for name in ("manifest.json", "corpus.jsonl", "queries.jsonl", "vectors.json"):
        assert (FIXTURES / name).is_file(), f"missing fixture file: {name}"
