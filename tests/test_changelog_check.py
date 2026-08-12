from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "tools" / "changelog_check.py"
PYPROJECT = REPO / "pyproject.toml"

WAIVER_NAME = "42.no-changelog.md"
REASON = "Internal refactor only; no user-visible behavior change."
FRAGMENT_NAME = "42.feature.md"
FRAGMENT_CONTENT = "Explainable retrieval reports why results were chosen."


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo_with_change(tmp_path: Path, filename: str, content: str) -> tuple[Path, str]:
    _run_git(tmp_path, "init", "--initial-branch=main")
    _run_git(tmp_path, "config", "user.email", "tsumugi@example.invalid")
    _run_git(tmp_path, "config", "user.name", "tsumugi")
    pyproject_copy = tmp_path / "pyproject.toml"
    pyproject_copy.write_text(PYPROJECT.read_text(encoding="utf-8"), encoding="utf-8")
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()
    _run_git(tmp_path, "add", "pyproject.toml", "changelog.d")
    _run_git(tmp_path, "commit", "-m", "baseline")
    base_sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    (changelog_dir / filename).write_text(content + "\n", encoding="utf-8")
    _run_git(tmp_path, "add", f"changelog.d/{filename}")
    _run_git(tmp_path, "commit", "-m", "add change")

    return tmp_path, base_sha


def test_changelog_check_accepts_single_nonempty_waiver(tmp_path: Path) -> None:
    repo, base_sha = _init_repo_with_change(tmp_path, WAIVER_NAME, REASON)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--dir", str(repo), "--base", base_sha],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "changelog-check: waiver 42 accepted\n"
    assert result.stderr == ""

    draft = subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--draft",
            "--config",
            str(repo / "pyproject.toml"),
            "--dir",
            str(repo),
        ],
        capture_output=True,
        text=True,
    )

    assert draft.returncode == 0, draft.stderr
    assert REASON not in draft.stdout


def test_changelog_check_accepts_single_ordinary_fragment(tmp_path: Path) -> None:
    repo, base_sha = _init_repo_with_change(tmp_path, FRAGMENT_NAME, FRAGMENT_CONTENT)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--dir", str(repo), "--base", base_sha],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "changelog-check: fragment 42.feature.md accepted\n"
    assert result.stderr == ""

    draft = subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--draft",
            "--config",
            str(repo / "pyproject.toml"),
            "--dir",
            str(repo),
        ],
        capture_output=True,
        text=True,
    )

    assert draft.returncode == 0, draft.stderr
    assert draft.stdout.count("Features") == 1, draft.stdout
    assert draft.stdout.count(FRAGMENT_CONTENT) == 1, draft.stdout


def test_changelog_check_accepts_multiple_ordinary_fragments(tmp_path: Path) -> None:
    _run_git(tmp_path, "init", "--initial-branch=main")
    _run_git(tmp_path, "config", "user.email", "tsumugi@example.invalid")
    _run_git(tmp_path, "config", "user.name", "tsumugi")
    pyproject_copy = tmp_path / "pyproject.toml"
    pyproject_copy.write_text(PYPROJECT.read_text(encoding="utf-8"), encoding="utf-8")
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()
    _run_git(tmp_path, "add", "pyproject.toml", "changelog.d")
    _run_git(tmp_path, "commit", "-m", "baseline")
    base_sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    feature = "Fix user-visible failure"
    bugfix = "Fix user-visible failure in CLI search"
    (changelog_dir / "42.feature.md").write_text(feature + "\n", encoding="utf-8")
    (changelog_dir / "43.bugfix.md").write_text(bugfix + "\n", encoding="utf-8")
    _run_git(tmp_path, "add", "changelog.d")
    _run_git(tmp_path, "commit", "-m", "add fragments")

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--dir", str(tmp_path), "--base", base_sha],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "changelog-check: 2 fragments accepted\n"
    assert result.stderr == ""

    draft = subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--draft",
            "--config",
            str(tmp_path / "pyproject.toml"),
            "--dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    assert draft.returncode == 0, draft.stderr
    assert draft.stdout.count("Features") == 1, draft.stdout
    assert draft.stdout.count("Bugfixes") == 1, draft.stdout
    assert draft.stdout.count(f"{feature} (#42)") == 1, draft.stdout
    assert draft.stdout.count(f"{bugfix} (#43)") == 1, draft.stdout
