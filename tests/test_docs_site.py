from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docs_site_builds_strict_from_navigation(tmp_path: Path) -> None:
    """The public MkDocs CLI must build the documented navigation strictly."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Shiori" in (site_dir / "index.html").read_text(encoding="utf-8")
