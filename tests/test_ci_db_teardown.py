from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tools" / "drop_isolated_db.sh"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "psql").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"current_database"* ]]; then
  printf '%s\\n' "${FAKE_CURRENT}"
else
  printf '%s\\n' "${FAKE_MARKER}"
fi
""",
        encoding="utf-8",
    )
    (bin_dir / "dropdb").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'dropped' > "${DROP_SENTINEL}"
""",
        encoding="utf-8",
    )
    for executable in (bin_dir / "psql", bin_dir / "dropdb"):
        executable.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    *,
    database: str,
    current: str,
    marker: str,
    expected_marker: str = "ci-123-1-456",
):
    bin_dir = _fake_bin(tmp_path)
    sentinel = tmp_path / "dropped"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CURRENT": current,
        "FAKE_MARKER": marker,
        "DROP_SENTINEL": str(sentinel),
        "SHIYI_TEST_DATABASE_NAME": database,
        "SHIYI_TEST_DATABASE_DSN": "postgresql://synthetic",
        "SHIYI_TEST_DATABASE_MARKER": expected_marker,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    result = subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, sentinel


def test_teardown_drops_only_matching_database_and_marker(tmp_path):
    database = "shiyi_test_123_1_456"
    result, sentinel = _run(
        tmp_path,
        database=database,
        current=database,
        marker="ci-123-1-456",
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "dropped"


def test_teardown_refuses_marker_mismatch(tmp_path):
    database = "shiyi_test_123_1_456"
    result, sentinel = _run(
        tmp_path,
        database=database,
        current=database,
        marker="ci-123-1-999",
    )

    assert result.returncode != 0
    assert not sentinel.exists()


def test_teardown_refuses_current_database_mismatch(tmp_path):
    database = "shiyi_test_123_1_456"
    result, sentinel = _run(
        tmp_path,
        database=database,
        current="another_database",
        marker="ci-123-1-456",
    )

    assert result.returncode != 0
    assert not sentinel.exists()


def test_teardown_refuses_database_outside_job_namespace(tmp_path):
    database = "shiyi_test_999_1_456"
    result, sentinel = _run(
        tmp_path,
        database=database,
        current=database,
        marker="ci-123-1-456",
    )

    assert result.returncode != 0
    assert not sentinel.exists()


def test_teardown_refuses_malformed_marker(tmp_path):
    database = "shiyi_test_123_1_456"
    result, sentinel = _run(
        tmp_path,
        database=database,
        current=database,
        marker="not-a-ci-marker",
        expected_marker="not-a-ci-marker",
    )

    assert result.returncode != 0
    assert not sentinel.exists()


def test_ci_marker_sql_uses_controlled_literal_not_command_variable_expansion():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    teardown = SCRIPT.read_text(encoding="utf-8")

    assert ":'marker'" not in workflow
    assert ":'marker'" not in teardown
    assert "VALUES ('${marker}')" in workflow
    assert "^ci-[0-9]+-[0-9]+-[0-9]+$" in workflow
    assert "^ci-[0-9]+-[0-9]+-[0-9]+$" in teardown
