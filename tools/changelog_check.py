#!/usr/bin/env python3
"""Public fail-closed changelog waiver check for the shiori repository.

Compares a real git base-to-HEAD diff and accepts exactly one non-empty
``changelog.d/<positive-integer>.no-changelog.md`` waiver when no ordinary
Towncrier fragment is present, verifying through the public Towncrier draft
that the ignore glob keeps the waiver out of the aggregated CHANGELOG.

Only this frozen single-waiver success path is implemented; every other
state fails closed with a single uniform ``changelog-check: rejected`` line
on stderr and exit code 1. Ordinary fragment acceptance, specific rejection
messages, and the remaining enforcement states are added by later TDD slices.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

WAIVER_RE = re.compile(r"^changelog\.d/([1-9][0-9]*)\.no-changelog\.md$")
REJECT = "changelog-check: rejected"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _changed_files(repo: Path, base: str) -> list[str]:
    return _git(repo, "diff", "--name-only", f"{base}...HEAD").splitlines()


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

    if has_ordinary_fragment or len(waivers) != 1:
        print(REJECT, file=sys.stderr)
        return 1

    waiver_path = repo / waivers[0]
    reason = waiver_path.read_text(encoding="utf-8").strip()
    if not reason:
        print(REJECT, file=sys.stderr)
        return 1

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
    if draft.returncode != 0 or reason in draft.stdout:
        print(REJECT, file=sys.stderr)
        return 1

    issue = WAIVER_RE.match(waivers[0]).group(1)
    print(f"changelog-check: waiver {issue} accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
