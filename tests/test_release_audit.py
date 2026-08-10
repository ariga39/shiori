from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "tools" / "release_audit.py"
SPEC = importlib.util.spec_from_file_location("release_audit", AUDIT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_audit = importlib.util.module_from_spec(SPEC)
sys.modules["release_audit"] = release_audit
SPEC.loader.exec_module(release_audit)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def test_safe_fixture_findings_are_retained_but_non_blocking() -> None:
    findings = release_audit._scan_text(
        "contact=test@example.invalid\nkey=sk_live_0123456789abcdef\npath=/home/u/project\npassword=shiyi-ci-only",
        object_prefix="a" * 40,
        source="reachable_blob",
        path="tests/fixture.py",
    )

    assert findings
    assert all(not finding.blocking for finding in findings)


def test_reachable_private_key_fails_closed_without_match_text(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "release@example.invalid")
    _git(tmp_path, "config", "user.name", "release-audit")
    (tmp_path / ".gitignore").write_text(
        ".env\n*.key\n*credentials*\n.data/\ndist/\nbuild/\n*.log\n",
        encoding="utf-8",
    )
    (tmp_path / "leaked.key").write_text(
        "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "add", "-f", "leaked.key")
    _git(tmp_path, "commit", "-m", "synthetic audit fixture")

    result = release_audit.audit(tmp_path)

    assert result["ok"] is False
    assert result["shallow"] is False
    assert result["reachable_commits"] >= 1
    assert result["blocking_finding_counts"]["private_key"] == 1
    finding = next(item for item in result["findings"] if item["category"] == "private_key")
    assert set(finding) == {"category", "object_prefix", "source", "blocking"}
    assert finding["object_prefix"] != "a" * 40


def test_artifact_audit_fails_closed_for_secret_shaped_output(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "release@example.invalid")
    _git(tmp_path, "config", "user.name", "release-audit")
    (tmp_path / ".gitignore").write_text(
        ".env\n*.key\n*credentials*\n.data/\ndist/\nbuild/\n*.log\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("synthetic repository\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "synthetic repository")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "summary.txt").write_text("api_key=not-a-real-secret-value\n", encoding="utf-8")

    result = release_audit.audit(tmp_path, artifact_dir=artifact)

    assert result["ok"] is False
    assert result["artifact_files"] == 1
    assert result["blocking_finding_counts"]["credential_assignment"] == 1
