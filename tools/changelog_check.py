#!/usr/bin/env python3
"""Public fail-closed changelog waiver check for the shiori repository.

Compares a real git base-to-HEAD diff and accepts exactly one non-empty
``changelog.d/<positive-integer>.no-changelog.md`` waiver when no ordinary
Towncrier fragment is present. The waiver must be ignored by the public
Towncrier draft so it never enters the aggregated CHANGELOG.

All other states fail closed (stderr + exit 1): the specific handling of
ordinary fragments, empty/multiple/missing waivers, and so on is added by
later TDD slices.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

WAIVER_RE = re.compile(r"^changelog\.d/([1-9][0-9]*)\.no-changelog\.md$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _changed_files(repo: Path, base: str) -> list[str]:
    return _git(repo, "diff", "--name-only", f"{base}...HEAD").splitlines()


def _fail(repo: Path, reason: str) -> int:
    print(f"changelog-check: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=".")
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.dir).resolve()
    changed = _changed_files(repo, args.base)

    waivers = [name for name in changed if WAIVER_RE.match(name)]
    has_ordinary_fragment = any(
        not WAIVER_RE.match(name) and name.startswith("changelog.d/") for name in changed
    )

    if has_ordinary_fragment:
        return _fail(repo, "ordinary fragment present (acceptance is a later slice)")
    if len(waivers) != 1:
        return _fail(repo, "expected exactly one no-changelog waiver")

    waiver_path = repo / waivers[0]
    reason = waiver_path.read_text(encoding="utf-8").strip()
    if not reason:
        return _fail(repo, "no-changelog waiver reason is empty")

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
    if draft.returncode != 0:
        return _fail(repo, f"towncrier draft failed: {draft.stderr.strip()}")
    if reason in draft.stdout:
        return _fail(repo, "waiver content leaked into the towncrier draft")

    issue = WAIVER_RE.match(waivers[0]).group(1)
    print(f"changelog-check: waiver {issue} accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
