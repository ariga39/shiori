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
