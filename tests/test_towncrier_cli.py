from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"


def test_towncrier_draft_aggregates_markdown_fragment(tmp_path: Path) -> None:
    """The public Towncrier CLI must aggregate a markdown fragment under the pinned config.

    A literal ``42.feature.md`` fragment placed in the configured fragments
    directory renders through ``python -m towncrier build --draft`` with the
    ``Features`` heading and the fragment content each appearing exactly once.
    """
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    content = "Changelog enforcement is now active."
    (fragment_dir / "42.feature.md").write_text(content + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "towncrier", "build", "--draft", "--config", str(PYPROJECT), "--dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Features") == 1, result.stdout
    assert result.stdout.count(content) == 1, result.stdout
