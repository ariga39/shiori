#!/usr/bin/env python3
"""Public fail-closed changelog check for the shiori repository.

Compares a real git base-to-HEAD diff and accepts exactly one of two frozen
success paths:

- a single non-empty ``changelog.d/<positive-integer>.no-changelog.md``
  waiver when no ordinary Towncrier fragment is present, verified through the
  public Towncrier draft so the ignore glob keeps the waiver out of the
  aggregate; or
- a single ``changelog.d/<positive-integer>.<type>.md`` ordinary fragment
  (type validity is enforced by the existing Towncrier configuration and
  strict draft) when no waiver is present, whose content appears in the draft.

Every other state fails closed with a single uniform ``changelog-check:
rejected`` line on stderr and exit code 1. Specific rejection messages and the
remaining enforcement states are added by later TDD slices.
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


def _draft(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=".")
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.dir).resolve()
    changed = _changed_files(repo, args.base)

    waivers = [name for name in changed if WAIVER_RE.match(name)]
    ordinary = [
        name
        for name in changed
        if name.startswith("changelog.d/") and not WAIVER_RE.match(name)
    ]

    draft = _draft(repo)
    if draft.returncode != 0:
        print(REJECT, file=sys.stderr)
        return 1

    if not ordinary and len(waivers) == 1:
        reason = (repo / waivers[0]).read_text(encoding="utf-8").strip()
        if reason and reason not in draft.stdout:
            issue = WAIVER_RE.match(waivers[0]).group(1)
            print(f"changelog-check: waiver {issue} accepted")
            return 0

    if not waivers and len(ordinary) == 1:
        content = (repo / ordinary[0]).read_text(encoding="utf-8").strip()
        if content and content in draft.stdout:
            print(f"changelog-check: fragment {Path(ordinary[0]).name} accepted")
            return 0

    print(REJECT, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
