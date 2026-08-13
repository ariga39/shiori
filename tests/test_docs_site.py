from __future__ import annotations

import re
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
    assert 'href="../CONFIGURATION/#test-database-isolation"' in english
    assert 'href="../CONFIGURATION/#' in chinese
    assert 'href="../zh-cn/' not in chinese


def test_starlight_cli_mcp_reference_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual CLI/MCP reference page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "cli-mcp-reference" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "cli-mcp-reference" / "index.html").read_text(encoding="utf-8")
    assert (
        "Both use the same configured search service, but their pagination surfaces "
        "are intentionally different." in english
    )
    assert "两者使用同一个已配置的搜索服务，但分页接口有意采用不同形式。" not in english
    assert "两者使用同一个已配置的搜索服务，但分页接口有意采用不同形式。" in chinese
    assert (
        "Both use the same configured search service, but their pagination surfaces "
        "are intentionally different." not in chinese
    )


def test_starlight_design_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual design page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "DESIGN" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "DESIGN" / "index.html").read_text(encoding="utf-8")
    assert (
        "Shiori turns conversation history into semantically searchable memory through "
        "an ingestion and query pipeline." in english
    )
    assert "Shiori 是一个将对话会话历史加工成语义可检索记忆的摄取与查询管线。" not in english
    assert "Shiori 是一个将对话会话历史加工成语义可检索记忆的摄取与查询管线。" in chinese
    assert (
        "Shiori turns conversation history into semantically searchable memory through "
        "an ingestion and query pipeline." not in chinese
    )
    assert (
        "The ingestion and retrieval pipelines do not use it, while privacy lifecycle "
        "operations still count, export, and delete legacy rows." in english
    )
    assert "摄取与检索管线不使用它，但隐私生命周期操作仍会统计、导出和删除 legacy 行。" not in english
    assert "摄取与检索管线不使用它，但隐私生命周期操作仍会统计、导出和删除 legacy 行。" in chinese
    assert (
        "The ingestion and retrieval pipelines do not use it, while privacy lifecycle "
        "operations still count, export, and delete legacy rows." not in chinese
    )


def test_starlight_atomic_rebuild_adr_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual atomic-rebuild ADR page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "adr" / "0001-atomic-rebuild-on-partial-embed-failure" / "index.html").read_text(
        encoding="utf-8"
    )
    chinese = (site_dir / "zh-cn" / "adr" / "0001-atomic-rebuild-on-partial-embed-failure" / "index.html").read_text(
        encoding="utf-8"
    )
    assert (
        "Choose option 1: atomic full rebuild. Embeddings are prepared before deletion, "
        "and delete plus insert remains transactional." in english
    )
    assert "选择方案 1：原子全量重建。嵌入在删除前准备完成，删除与插入保持事务原子性。" not in english
    assert "选择方案 1：原子全量重建。嵌入在删除前准备完成，删除与插入保持事务原子性。" in chinese
    assert (
        "Choose option 1: atomic full rebuild. Embeddings are prepared before deletion, "
        "and delete plus insert remains transactional." not in chinese
    )


def test_starlight_release_checklist_is_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render a bilingual release checklist page."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    english = (site_dir / "RELEASE_CHECKLIST" / "index.html").read_text(encoding="utf-8")
    chinese = (site_dir / "zh-cn" / "RELEASE_CHECKLIST" / "index.html").read_text(encoding="utf-8")
    assert "This is a release-candidate checklist, not a release authorization." in english
    assert "这是一份候选发布检查清单，不是发布授权。" not in english
    assert "这是一份候选发布检查清单，不是发布授权。" in chinese
    assert "This is a release-candidate checklist, not a release authorization." not in chinese
    assert (
        "Record the actual protected merge SHA after exact-head gates and "
        "pair-programming review are green." in english
    )
    assert "在精确 head 门与结对编程 review 全绿后，记录实际受保护 merge SHA。" not in english
    assert "在精确 head 门与结对编程 review 全绿后，记录实际受保护 merge SHA。" in chinese
    assert (
        "Record the actual protected merge SHA after exact-head gates and "
        "pair-programming review are green." not in chinese
    )


def test_starlight_preserves_case_sensitive_stable_routes(tmp_path: Path) -> None:
    """The public Starlight build must emit the frozen case-sensitive routes."""
    site_dir = tmp_path / "site"

    result = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    generated = {
        path.relative_to(site_dir).as_posix()
        for path in site_dir.rglob("index.html")
    }
    expected = {
        "CONFIGURATION/index.html",
        "DESIGN/index.html",
        "RELEASE_CHECKLIST/index.html",
        "zh-cn/CONFIGURATION/index.html",
        "zh-cn/DESIGN/index.html",
        "zh-cn/RELEASE_CHECKLIST/index.html",
    }
    missing = sorted(expected - generated)
    assert not missing, f"case-sensitive stable routes missing: {missing}"


def test_starlight_navigation_is_explicit_and_bilingual(tmp_path: Path) -> None:
    """The public Starlight build must render an explicit bilingual sidebar."""
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

    def sidebar(page: str) -> str:
        start = page.index('<nav class="sidebar')
        return page[start : page.index("</nav>", start) + len("</nav>")]

    en_sidebar = sidebar(english)
    zh_sidebar = sidebar(chinese)

    for group in ("Start", "User guide", "Project"):
        assert group in en_sidebar
        assert group not in zh_sidebar
    for group in ("开始", "用户指南", "项目"):
        assert group in zh_sidebar
        assert group not in en_sidebar

    en_groups = [g for g in ("Start", "User guide", "Project") if g in en_sidebar]
    assert [en_sidebar.index(g) for g in en_groups] == sorted(
        en_sidebar.index(g) for g in en_groups
    ), en_groups
    zh_groups = [g for g in ("开始", "用户指南", "项目") if g in zh_sidebar]
    assert [zh_sidebar.index(g) for g in zh_groups] == sorted(
        zh_sidebar.index(g) for g in zh_groups
    ), zh_groups

    en_links = [
        "/",
        "/getting-started/",
        "/CONFIGURATION/",
        "/privacy-policy/",
        "/cli-mcp-reference/",
        "/DESIGN/",
        "/contributing/",
        "/adr/0001-atomic-rebuild-on-partial-embed-failure/",
        "/RELEASE_CHECKLIST/",
    ]
    zh_links = [f"/zh-cn{link}" if link != "/" else "/zh-cn/" for link in en_links]

    en_positions = [en_sidebar.find(f'href="{link}"') for link in en_links]
    assert all(position >= 0 for position in en_positions), en_positions
    assert en_positions == sorted(en_positions), en_positions
    zh_positions = [zh_sidebar.find(f'href="{link}"') for link in zh_links]
    assert all(position >= 0 for position in zh_positions), zh_positions
    assert zh_positions == sorted(zh_positions), zh_positions

    assert re.search(r'<option value="/"[^>]*>English</option>', english)
    assert re.search(r'<option value="/zh-cn/"[^>]*>简体中文</option>', english)
    assert re.search(r'<option value="/"[^>]*>English</option>', chinese)
    assert re.search(r'<option value="/zh-cn/"[^>]*>简体中文</option>', chinese)


def test_starlight_llms_txt_is_bilingual_and_public(tmp_path: Path) -> None:
    """The public build must ship a bilingual llms.txt identical to the root copy."""
    check = subprocess.run(
        [sys.executable, "tools/build_llms_txt.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert check.returncode == 0, check.stderr
    assert check.stdout == "llms.txt is up to date\n"

    site_dir = tmp_path / "site"
    build = subprocess.run(
        ["npm", "run", "docs:build", "--", "--outDir", str(site_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert build.returncode == 0, build.stderr
    root_llms = (ROOT / "llms.txt").read_bytes()
    site_llms = (site_dir / "llms.txt").read_bytes()
    assert root_llms == site_llms

    rendered = root_llms.decode("utf-8")
    assert "# Shiori" in rendered
    assert "> Searchable long-term memory for AI agents." in rendered
    assert "## English" in rendered
    assert "## 简体中文" in rendered
    assert "https://raw.githubusercontent.com/ariga39/shiori/main/docs/" not in rendered

    expected_urls = [
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/index.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/getting-started.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/CONFIGURATION.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/privacy-policy.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/cli-mcp-reference.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/DESIGN.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/contributing.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/adr/0001-atomic-rebuild-on-partial-embed-failure.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/RELEASE_CHECKLIST.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/index.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/getting-started.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/CONFIGURATION.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/privacy-policy.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/cli-mcp-reference.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/DESIGN.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/contributing.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/adr/0001-atomic-rebuild-on-partial-embed-failure.md",
        "https://raw.githubusercontent.com/ariga39/shiori/main/src/content/docs/zh-cn/RELEASE_CHECKLIST.md",
    ]
    positions = [rendered.find(url) for url in expected_urls]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions), positions
