from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docs_site_builds_strict_from_navigation(tmp_path: Path) -> None:
    """The public MkDocs CLI must build the documented navigation strictly."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Shiori" in (site_dir / "index.html").read_text(encoding="utf-8")


def test_docs_site_navigation_exposes_existing_markdown(tmp_path: Path) -> None:
    """The built homepage must expose every existing documentation page in order."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    homepage = (site_dir / "index.html").read_text(encoding="utf-8")
    expected_hrefs = [
        'href="."',
        'href="CONFIGURATION/"',
        'href="privacy-policy/"',
        'href="DESIGN/"',
        'href="adr/0001-atomic-rebuild-on-partial-embed-failure/"',
        'href="RELEASE_CHECKLIST/"',
    ]
    positions = [homepage.find(href) for href in expected_hrefs]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions), positions


def test_docs_getting_started_covers_supported_lifecycle(tmp_path: Path) -> None:
    """The public guide must cover the supported install-to-serve lifecycle."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    homepage = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="getting-started/"' in homepage

    guide = (site_dir / "getting-started" / "index.html").read_text(encoding="utf-8")
    for heading in ("install", "configure", "migrate", "ingest", "query", "serve"):
        assert f'id="{heading}">{heading.title()}</h2>' in guide
    for command in (
        "uv sync --locked --extra dev",
        "shiori db migrate",
        "shiori ingest --source sessions",
        "shiori query",
        "shiori serve",
    ):
        assert command in guide


def test_docs_contributing_covers_local_development_workflow(tmp_path: Path) -> None:
    """The contributor guide must document the supported local workflow."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    homepage = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="contributing/"' in homepage

    guide = (site_dir / "contributing" / "index.html").read_text(encoding="utf-8")
    for heading, title in (
        ("development-setup", "Development setup"),
        ("tests", "Tests"),
        ("documentation", "Documentation"),
        ("pull-requests", "Pull requests"),
    ):
        assert f'id="{heading}">{title}</h2>' in guide
    for command in (
        "uv sync --locked --extra dev",
        "uv run pytest -q",
        "uv run mkdocs build --strict",
        "uv run mkdocs serve",
    ):
        assert command in guide


def test_docs_cli_mcp_reference_matches_public_surfaces(tmp_path: Path) -> None:
    """The CLI/MCP reference must distinguish their real pagination surfaces."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    homepage = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="cli-mcp-reference/"' in homepage

    reference = (site_dir / "cli-mcp-reference" / "index.html").read_text(encoding="utf-8")
    for heading, title in (
        ("cli-commands", "CLI commands"),
        ("query-options", "Query options"),
        ("mcp-search", "MCP search"),
        ("limits-and-errors", "Limits and errors"),
    ):
        assert f'id="{heading}">{title}</h2>' in reference
    for literal in (
        "shiori ingest --source sessions",
        "shiori query",
        "--limit",
        "--explain",
        "shiori serve",
        "search",
        "offset",
        "has_more",
        "next_offset",
    ):
        assert literal in reference
    assert "--offset" not in reference


def test_llms_txt_matches_documentation_navigation() -> None:
    """The public checker must verify deterministic LLM-readable documentation."""
    result = subprocess.run(
        [sys.executable, "tools/build_llms_txt.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "llms.txt is up to date\n"

    root_llms = (ROOT / "llms.txt").read_bytes()
    docs_llms = (ROOT / "docs" / "llms.txt").read_bytes()
    assert root_llms == docs_llms

    rendered = root_llms.decode("utf-8")
    assert "# Shiori" in rendered
    assert "> Searchable long-term memory for AI agents." in rendered
    expected_urls = [
        "https://raw.githubusercontent.com/ariga39/shiori/main/docs/getting-started.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/docs/contributing.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/docs/cli-mcp-reference.md",
    ]
    positions = [rendered.find(url) for url in expected_urls]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions), positions


def test_llms_txt_write_is_deterministic(tmp_path: Path) -> None:
    """The public writer must deterministically regenerate both llms.txt copies."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("# Fixture Docs\n", encoding="utf-8")
    (tmp_path / "mkdocs.yml").write_text(
        """site_name: Fixture Docs
extra:
  raw_docs_base_url: https://example.invalid/docs/
nav:
  - Home: index.md
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "fixture-docs"
version = "0.0.0"
description = "Fixture documentation"
requires-python = ">=3.11"
""",
        encoding="utf-8",
    )
    expected = b"""# Fixture Docs

> Fixture documentation.

## Documentation

- [Home](https://example.invalid/docs/index.md): Raw Markdown source.
"""
    command = [
        sys.executable,
        str(ROOT / "tools" / "build_llms_txt.py"),
        "--write",
        "--dir",
        str(tmp_path),
    ]

    first = subprocess.run(command, capture_output=True, text=True, check=False)

    assert first.returncode == 0, first.stderr
    assert first.stdout == "wrote llms.txt and docs/llms.txt\n"
    assert first.stderr == ""
    assert (tmp_path / "llms.txt").read_bytes() == expected
    assert (tmp_path / "docs" / "llms.txt").read_bytes() == expected

    second = subprocess.run(command, capture_output=True, text=True, check=False)

    assert second.returncode == 0, second.stderr
    assert second.stdout == "wrote llms.txt and docs/llms.txt\n"
    assert second.stderr == ""
    assert (tmp_path / "llms.txt").read_bytes() == expected
    assert (tmp_path / "docs" / "llms.txt").read_bytes() == expected


def test_docs_check_builds_site_and_llms_index(tmp_path: Path) -> None:
    """The public docs check must validate and build both human and LLM docs."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "docs_check.py"),
            "--site-dir",
            str(site_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "docs-check: ok\n"
    assert result.stderr == ""
    assert "Shiori" in (site_dir / "index.html").read_text(encoding="utf-8")
    assert (site_dir / "llms.txt").read_bytes() == (ROOT / "llms.txt").read_bytes()


def test_docs_site_has_no_missing_internal_anchors(tmp_path: Path) -> None:
    """The public strict build must not report missing internal anchors."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(tmp_path / "site"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "does not contain an anchor" not in result.stderr


def test_docs_contributing_explains_changelog_fragments(tmp_path: Path) -> None:
    """The strict-built contributing guide must explain the changelog contract."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(tmp_path / "site"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    page = (tmp_path / "site" / "contributing" / "index.html").read_text(encoding="utf-8")
    assert 'id="changelog-fragments">Changelog fragments</h2>' in page
    assert "changelog.d/&lt;issue&gt;.&lt;type&gt;.md" in page
    assert "changelog.d/&lt;issue&gt;.no-changelog.md" in page
    assert "User-visible changes require at least one changelog fragment." in page
    assert (
        "Internal or test-only pull requests may instead use exactly one non-empty waiver."
        in page
    )
    assert (
        "The waiver must explain why no user-facing changelog entry is needed and must "
        "not be mixed with ordinary fragments."
        in page
    )


def test_starlight_site_builds_bilingual_homepages(tmp_path: Path) -> None:
    """The public Starlight build must render English and Simplified Chinese homepages."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "index.html").read_text(encoding="utf-8")
    assert "Searchable long-term memory for AI agents." in english
    assert "面向 AI 智能体的可搜索长期记忆。" not in english
    assert "面向 AI 智能体的可搜索长期记忆。" in chinese
    assert "Searchable long-term memory for AI agents." not in chinese


def test_starlight_getting_started_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual getting-started page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "getting-started" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "getting-started" / "index.html").read_text(encoding="utf-8")
    assert (
        "This guide follows Shiori's supported local lifecycle from a locked development "
        "install through its read-only MCP server." in english
    )
    assert "本指南涵盖 Shiori 从锁定的开发环境安装到只读 MCP 服务器的受支持本地生命周期。" not in english
    assert "本指南涵盖 Shiori 从锁定的开发环境安装到只读 MCP 服务器的受支持本地生命周期。" in chinese
    assert (
        "This guide follows Shiori's supported local lifecycle from a locked development "
        "install through its read-only MCP server." not in chinese
    )
    for page in (english, chinese):
        assert 'href="../CONFIGURATION/"' in page
        assert 'href="../privacy-policy/"' in page


def test_starlight_configuration_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual configuration page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "CONFIGURATION" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "CONFIGURATION" / "index.html").read_text(encoding="utf-8")
    assert "This is the current runtime contract." in english
    assert "这是当前的运行时契约。" not in english
    assert "这是当前的运行时契约。" in chinese
    assert "This is the current runtime contract." not in chinese


def test_starlight_privacy_policy_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual privacy policy page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "privacy-policy" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "privacy-policy" / "index.html").read_text(encoding="utf-8")
    assert (
        "This document states the local data-minimization and lifecycle contract that "
        "the ingestion and privacy seams enforce." in english
    )
    assert "本文说明摄取与隐私边界所执行的本地数据最小化与生命周期契约。" not in english
    assert "本文说明摄取与隐私边界所执行的本地数据最小化与生命周期契约。" in chinese
    assert (
        "This document states the local data-minimization and lifecycle contract that "
        "the ingestion and privacy seams enforce." not in chinese
    )


def test_starlight_contributing_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual contributing page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "contributing" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "contributing" / "index.html").read_text(encoding="utf-8")
    assert (
        "Keep changes narrowly scoped, test them through public behavior, and report "
        "what was and was not verified." in english
    )
    assert "保持变更范围精简，通过公开行为进行测试，并报告哪些内容已验证、哪些尚未验证。" not in english
    assert "保持变更范围精简，通过公开行为进行测试，并报告哪些内容已验证、哪些尚未验证。" in chinese
    assert (
        "Keep changes narrowly scoped, test them through public behavior, and report "
        "what was and was not verified." not in chinese
    )
