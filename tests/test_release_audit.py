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
    private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
    private_key_end = "-----END " + "PRIVATE KEY-----"
    (tmp_path / "leaked.key").write_text(
        f"{private_key_marker}\nsynthetic\n{private_key_end}\n",
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
    secret_name = "api" + "_key"
    secret_value = "not-a-real-" + "secret-value"
    (artifact / "summary.txt").write_text(f"{secret_name}={secret_value}\n", encoding="utf-8")

    result = release_audit.audit(tmp_path, artifact_dir=artifact)

    assert result["ok"] is False
    assert result["artifact_files"] == 1
    assert result["blocking_finding_counts"]["credential_assignment"] == 1


def test_audit_covers_all_refs_and_commit_metadata(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "release@example.invalid")
    _git(tmp_path, "config", "user.name", "release-audit")
    (tmp_path / ".gitignore").write_text(
        ".env\n*.key\n*credentials*\n.data/\ndist/\nbuild/\n*.log\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("main\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "main commit")

    _git(tmp_path, "checkout", "-b", "side")
    (tmp_path / "side.md").write_text("side\n", encoding="utf-8")
    _git(tmp_path, "add", "side.md")
    _git(tmp_path, "commit", "-m", "side contact=test@example.invalid")
    _git(tmp_path, "tag", "v0.0.1")
    _git(tmp_path, "checkout", "main")

    result = release_audit.audit(tmp_path)

    assert result["shallow"] is False
    assert result["reachable_refs"] >= 3
    assert result["reachable_commits"] >= 2
    assert any(item["source"] == "commit_metadata" for item in result["findings"])
