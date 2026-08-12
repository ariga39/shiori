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


def _make_baseline_project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "pyproject.toml").write_text(PYPROJECT.read_text(encoding="utf-8"), encoding="utf-8")
    fragment_dir = project / "changelog.d"
    fragment_dir.mkdir()
    (fragment_dir / "42.feature.md").write_text(
        "Manual feature bullet.\n", encoding="utf-8"
    )
    (project / "CHANGELOG.md").write_text(
        "# Changelog\n"
        "\n"
        "<!-- towncrier release notes start -->\n"
        "\n"
        "## 0.1.0 (unreleased)\n"
        "\n"
        "### Added\n"
        "\n"
        "- Existing history bullet.\n",
        encoding="utf-8",
    )
    return project


def test_towncrier_build_is_deterministic_with_keep(tmp_path: Path) -> None:
    """The public build must deterministically prepend a versioned section and keep history."""
    first = _make_baseline_project(tmp_path, "one")
    second = _make_baseline_project(tmp_path, "two")

    first_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--version",
            "0.1.1",
            "--date",
            "2026-08-13",
            "--keep",
            "--config",
            str(first / "pyproject.toml"),
            "--dir",
            str(first),
        ],
        capture_output=True,
        text=True,
    )
    second_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--version",
            "0.1.1",
            "--date",
            "2026-08-13",
            "--keep",
            "--config",
            str(second / "pyproject.toml"),
            "--dir",
            str(second),
        ],
        capture_output=True,
        text=True,
    )

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first_result.stderr == ""
    assert second_result.stderr == ""

    first_text = (first / "CHANGELOG.md").read_bytes()
    second_text = (second / "CHANGELOG.md").read_bytes()
    assert first_text == second_text

    text = first_text.decode("utf-8")
    assert "# Changelog" in text
    assert "<!-- towncrier release notes start -->" in text
    assert "## 0.1.1 (2026-08-13)" in text
    assert "### Features" in text
    assert "Manual feature bullet." in text
    assert "## 0.1.0 (unreleased)" in text
    assert text.find("# Changelog") < text.find("<!-- towncrier release notes start -->")
    assert text.find("<!-- towncrier release notes start -->") < text.find("## 0.1.1 (2026-08-13)")
    assert text.find("## 0.1.1 (2026-08-13)") < text.find("### Features")
    assert text.find("### Features") < text.find("Manual feature bullet.")
    assert text.find("Manual feature bullet.") < text.find("## 0.1.0 (unreleased)")

    assert (first / "changelog.d" / "42.feature.md").exists()
    assert (second / "changelog.d" / "42.feature.md").exists()
