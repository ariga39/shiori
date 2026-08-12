#!/usr/bin/env python3
"""Check the deterministic llms.txt index derived from MkDocs navigation."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mkdocs.yml"
OUTPUTS = (ROOT / "llms.txt", ROOT / "docs" / "llms.txt")


class LlmsTxtError(ValueError):
    """The documentation navigation cannot produce a safe llms.txt index."""


def _pages(items: object) -> list[tuple[str, str]]:
    if not isinstance(items, list):
        raise LlmsTxtError("navigation must be a list")

    pages: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or len(item) != 1:
            raise LlmsTxtError("navigation entries must be single-key mappings")
        label, target = next(iter(item.items()))
        if not isinstance(label, str) or not label.strip():
            raise LlmsTxtError("navigation labels must be non-empty strings")
        if isinstance(target, list):
            pages.extend(_pages(target))
        elif isinstance(target, str):
            pages.append((label, target))
        else:
            raise LlmsTxtError("navigation targets must be Markdown paths or lists")
    return pages


def _validated_pages(nav: object) -> list[tuple[str, str]]:
    pages = _pages(nav)
    seen: set[str] = set()
    docs_root = (ROOT / "docs").resolve()

    for _, target in pages:
        parsed = urlparse(target)
        path = PurePosixPath(target)
        if parsed.scheme or parsed.netloc or path.is_absolute() or path.suffix.lower() != ".md":
            raise LlmsTxtError("navigation contains an external or non-Markdown target")
        if target in seen:
            raise LlmsTxtError("navigation contains a duplicate target")
        seen.add(target)

        source = (docs_root / target).resolve()
        if not source.is_relative_to(docs_root) or not source.is_file():
            raise LlmsTxtError("navigation target is missing or outside docs")
    return pages


def _render() -> str:
    config: dict[str, Any] = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base_url = config.get("extra", {}).get("raw_docs_base_url")
    if not isinstance(base_url, str) or not base_url.endswith("/"):
        raise LlmsTxtError("raw_docs_base_url must be an absolute directory URL")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise LlmsTxtError("raw_docs_base_url must be an absolute directory URL")

    lines = [
        f"# {config['site_name']}",
        "",
        f"> {str(project['description']).rstrip('.')}.",
        "",
        "## Documentation",
        "",
    ]
    for label, target in _validated_pages(config.get("nav")):
        lines.append(f"- [{label}]({base_url}{quote(target, safe='/')}): Raw Markdown source.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("--check is required")

    try:
        expected = _render().encode("utf-8")
        if any(path.read_bytes() != expected for path in OUTPUTS):
            raise LlmsTxtError("generated files differ from documentation navigation")
    except (KeyError, LlmsTxtError, OSError, tomllib.TOMLDecodeError, yaml.YAMLError):
        print("llms.txt is out of date", file=sys.stderr)
        return 1

    print("llms.txt is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
