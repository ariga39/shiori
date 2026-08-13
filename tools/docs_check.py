#!/usr/bin/env python3
"""Run the complete local documentation validation surface."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> int:
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    commands = [
        [sys.executable, str(ROOT / "tools" / "build_llms_txt.py"), "--check"],
        ["npm", "run", "docs:build", "--", "--outDir", str(args.site_dir.resolve())],
    ]
    for command in commands:
        returncode = _run(command)
        if returncode != 0:
            return returncode

    print("docs-check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
