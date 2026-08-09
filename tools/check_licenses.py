"""Fail-closed license metadata check for direct locked dependencies."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

EXPECTED: dict[str, tuple[str, ...]] = {
    "mcp": ("MIT",),
    "numpy": ("BSD-3-Clause", "0BSD", "MIT", "Zlib", "CC0-1.0"),
    "pyright": ("MIT",),
    "psycopg2-binary": ("LGPL with exceptions",),
    "requests": ("Apache-2.0",),
    "tiktoken": ("MIT",),
    "pytest": ("MIT",),
    "ruff": ("MIT",),
}

LOCK_FILE = Path(__file__).resolve().parents[1] / "uv.lock"


def _locked_packages() -> set[str]:
    try:
        lock = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot read lock file: {LOCK_FILE}") from exc
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock has no package table")
    names = {item.get("name") for item in packages if isinstance(item, dict)}
    return {name for name in names if isinstance(name, str)}


def _declared_license(package: str) -> str:
    try:
        info = metadata(package)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"{package} is not installed") from exc
    expressions = info.get_all("License-Expression") or []
    legacy = info.get_all("License") or []
    values = [value.strip() for value in [*expressions, *legacy] if value and value.strip()]
    if not values:
        raise RuntimeError(f"{package} has no declared license metadata")
    return " | ".join(values)


def main() -> int:
    failures: list[str] = []
    try:
        locked = _locked_packages()
    except RuntimeError as exc:
        failures.append(str(exc))
        locked = set()
    for package, expected in EXPECTED.items():
        if package not in locked:
            failures.append(f"{package} is not present in uv.lock")
            continue
        try:
            declared = _declared_license(package)
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        if not any(marker.lower() in declared.lower() for marker in expected):
            failures.append(f"{package} declares {declared!r}; expected one of {expected!r}")
    if failures:
        for failure in failures:
            print(f"license check failed: {failure}")
        return 1
    print(f"license metadata ok: {len(EXPECTED)} direct dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
