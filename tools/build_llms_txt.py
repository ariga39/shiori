#!/usr/bin/env python3
"""Check the deterministic llms.txt index derived from the single nav source.

When a repository-root ``docs-navigation.json`` exists, the index is derived
from that Starlight navigation (bilingual English + Simplified Chinese). The
legacy MkDocs renderer remains as a fallback used only when the JSON is absent,
so the isolated legacy fixture keeps its original behavior and outputs.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]


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


def _validated_pages(nav: object, root: Path) -> list[tuple[str, str]]:
    pages = _pages(nav)
    seen: set[str] = set()
    docs_root = (root / "docs").resolve()

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


def _render(root: Path) -> str:
    config: dict[str, Any] = yaml.safe_load(
        (root / "mkdocs.yml").read_text(encoding="utf-8")
    )
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
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
    for label, target in _validated_pages(config.get("nav"), root):
        lines.append(f"- [{label}]({base_url}{quote(target, safe='/')}): Raw Markdown source.")
    return "\n".join(lines) + "\n"


def _frontmatter_title(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise LlmsTxtError(f"missing frontmatter: {path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise LlmsTxtError(f"unterminated frontmatter: {path}")
    frontmatter = yaml.safe_load(text[4:end])
    title = frontmatter.get("title") if isinstance(frontmatter, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise LlmsTxtError(f"frontmatter title missing or empty: {path}")
    return title.strip()


def _starlight_nav(root: Path) -> tuple[str, list[str]]:
    nav = json.loads((root / "docs-navigation.json").read_text(encoding="utf-8"))
    if not isinstance(nav, dict):
        raise LlmsTxtError("docs-navigation.json must contain an object")

    raw_base = nav.get("rawBase")
    if not isinstance(raw_base, str) or not raw_base.endswith("/"):
        raise LlmsTxtError("rawBase must be an absolute directory URL")
    parsed_base = urlparse(raw_base)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise LlmsTxtError("rawBase must be an absolute https directory URL")

    sidebar = nav.get("sidebar")
    if not isinstance(sidebar, list) or not sidebar:
        raise LlmsTxtError("sidebar must be a non-empty list")

    slugs: list[str] = []
    for group in sidebar:
        if not isinstance(group, dict):
            raise LlmsTxtError("sidebar groups must be objects")
        label = group.get("label")
        if not isinstance(label, str) or not label.strip():
            raise LlmsTxtError("sidebar group labels must be non-empty strings")
        items = group.get("items")
        if not isinstance(items, list) or not items:
            raise LlmsTxtError("sidebar group items must be non-empty lists")
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise LlmsTxtError("sidebar items must be non-empty slugs")
            parsed_item = urlparse(item)
            path = PurePosixPath(item)
            if parsed_item.scheme or parsed_item.netloc or path.is_absolute():
                raise LlmsTxtError("sidebar items must be relative slugs")
            if ".." in item.split("/"):
                raise LlmsTxtError("sidebar items must not traverse directories")
            if item in slugs:
                raise LlmsTxtError("sidebar items must be unique")
            slugs.append(item)

    content_root = (root / "src" / "content" / "docs").resolve()
    for slug in slugs:
        for prefix in ("", "zh-cn/"):
            source = (content_root / f"{prefix}{slug}.md").resolve()
            if not source.is_relative_to(content_root) or not source.is_file():
                raise LlmsTxtError(
                    f"missing docs source for slug {slug!r} in {prefix or 'root'} locale"
                )
    return raw_base, slugs


def _starlight_render(root: Path) -> str:
    raw_base, slugs = _starlight_nav(root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    description = str(project["description"]).rstrip(".") + "."
    content_root = root / "src" / "content" / "docs"

    lines = ["# Shiori", "", f"> {description}", "", "## English", ""]
    for slug in slugs:
        title = _frontmatter_title(content_root / f"{slug}.md")
        lines.append(
            f"- [{title}]({raw_base}{quote(slug, safe='/')}.md): Raw Markdown source."
        )
    lines.append("")
    lines.append("## 简体中文")
    lines.append("")
    for slug in slugs:
        title = _frontmatter_title(content_root / "zh-cn" / f"{slug}.md")
        lines.append(
            f"- [{title}]({raw_base}zh-cn/{quote(slug, safe='/')}.md): Raw Markdown source."
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--dir", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.dir.resolve()

    if (root / "docs-navigation.json").is_file():
        renderer = _starlight_render
        outputs = (
            root / "llms.txt",
            root / "docs" / "llms.txt",
            root / "public" / "llms.txt",
        )
        write_message = "wrote llms.txt, docs/llms.txt and public/llms.txt"
    else:
        renderer = _render
        outputs = (root / "llms.txt", root / "docs" / "llms.txt")
        write_message = "wrote llms.txt and docs/llms.txt"

    try:
        expected = renderer(root).encode("utf-8")
        if args.write:
            for path in outputs:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(expected)
            print(write_message)
            return 0
        if any(path.read_bytes() != expected for path in outputs):
            raise LlmsTxtError("generated files differ from documentation navigation")
    except (
        KeyError,
        LlmsTxtError,
        OSError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
        json.JSONDecodeError,
    ):
        print("llms.txt is out of date", file=sys.stderr)
        return 1

    print("llms.txt is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
