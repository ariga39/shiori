#!/usr/bin/env python3
"""Check the deterministic llms.txt index derived from the shared navigation.

The index is derived from ``docs-site/docs-navigation.json``, which is also
consumed by the contained Astro Starlight project, and the bilingual end-user
Markdown sources under the repository ``docs/`` directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]


class LlmsTxtError(ValueError):
    """The documentation navigation cannot produce a safe llms.txt index."""


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
    nav = json.loads(
        (root / "docs-site" / "docs-navigation.json").read_text(encoding="utf-8")
    )
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
        translations = group.get("translations")
        if not isinstance(translations, dict):
            raise LlmsTxtError("sidebar group translations must be a mapping")
        zh_label = translations.get("zh-CN")
        if not isinstance(zh_label, str) or not zh_label.strip():
            raise LlmsTxtError(
                "sidebar group translations must include a non-empty zh-CN label"
            )
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

    content_root = (root / "docs").resolve()
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
    content_root = root / "docs"

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

    outputs = (root / "llms.txt", root / "docs-site" / "public" / "llms.txt")
    write_message = "wrote llms.txt and docs-site/public/llms.txt"

    try:
        expected = _starlight_render(root).encode("utf-8")
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
