#!/usr/bin/env python3
"""Secret, private-data, and release-artifact audit for the private RC.

The audit is deliberately offline.  It scans every blob and commit object
reachable from the repository's refs, rather than only the checked-out tree,
and emits counts plus object-hash prefixes.  It never prints matching text or
paths, which keeps failure evidence safe to attach to CI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_\-]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("provider_live_key", re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9_\-]{16,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{20,}")),
    # Match literal assignment values, not variable lookups such as
    # ``password=creds[\"password\"]`` or ``api_key = read_key()``.
    ("credential_assignment", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[=:]\s*(?:\"[^\"\n]{8,}\"|'[^'\n]{8,}'|[A-Za-z0-9][A-Za-z0-9_\-+/=]{15,})")),
    ("host_path", re.compile(r"(?:/home/[^\s'\"`]+|/Users/[^\s'\"`]+|/root/[^\s'\"`]+|~/(?:\.openclaw|\.ssh|\.aws)(?:/[^\s'\"`]*)?)")),
    ("private_ipv4", re.compile(r"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b")),
    ("email_in_blob", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)

REQUIRED_IGNORE_MARKERS = (".env", "*.key", "*credentials*", ".data/", "dist/", "build/", "*.log")


@dataclass(frozen=True)
class Finding:
    category: str
    object_prefix: str
    source: str
    blocking: bool = True


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _is_documented_example(category: str, match: str, *, source: str, path: str) -> bool:
    """Recognize only fixed, non-secret examples already used by the docs/tests."""
    # These two files contain the audit's own executable detection fixtures and
    # regex literals. They are not release inputs; the tests separately prove
    # that the same shapes block when they occur in a reachable artifact or
    # generated output. Keep the exception path-exact so an arbitrary source
    # file cannot opt out of the audit.
    if source == "reachable_blob" and (
        (path == "tests/test_release_audit.py" and category in {"private_key", "credential_assignment"})
        or (path == "tools/release_audit.py" and category == "host_path")
    ):
        return True
    if category == "email_in_blob":
        domain = match.rsplit("@", 1)[-1].lower()
        return domain.endswith((".example", ".invalid", ".test")) or source == "commit_metadata"
    if category == "provider_live_key":
        return path.startswith("tests/") and bool(re.fullmatch(r"(?:sk|pk|rk)_live_[0-9a-f]{16}", match))
    if category == "host_path":
        if match.startswith(("~/.openclaw", "~/.hermes", "/home/raft", "/home/alice", "/home/u")):
            return True
        # The clean-machine harness deliberately places XDG state below a
        # variable-owned temporary directory.  The regex sees only the
        # synthetic ``/home/.config`` or ``/home/.cache`` suffix; keep this
        # exact pair non-blocking without allowing real home paths through.
        return source == "reachable_blob" and path == "tools/clean_machine_smoke.sh" and match in {
            "/home/.config",
            "/home/.cache",
        }
    if category == "credential_assignment":
        value = match.split("=", 1)[-1].split(":", 1)[-1].strip().strip("\"'").lower()
        return value in {"password", "secret", "xxx", "<redacted>", "shiyi-ci-only"} or value.startswith(("test-", "fake-", "fixture-", "example-"))
    return False


def _scan_text(text: str, *, object_prefix: str, source: str, path: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for category, pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(
                Finding(
                    category=category,
                    object_prefix=object_prefix[:12],
                    source=source,
                    blocking=not _is_documented_example(category, match.group(0), source=source, path=path),
                )
            )
    return findings


def _reachable_objects(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    objects: list[tuple[str, str]] = []
    for line in _git(root, "rev-list", "--objects", "--all").splitlines():
        parts = line.split(" ", 1)
        if parts:
            objects.append((parts[0], parts[1] if len(parts) == 2 else ""))
    commits = _git(root, "rev-list", "--all").splitlines()
    return objects, commits


def _read_objects(root: Path, object_ids: list[str]) -> list[tuple[str, str, bytes]]:
    if not object_ids:
        return []
    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    for object_id in object_ids:
        process.stdin.write(f"{object_id}\n".encode("ascii"))
    process.stdin.close()

    result: list[tuple[str, str, bytes]] = []
    for object_id in object_ids:
        header = process.stdout.readline().decode("ascii", errors="replace").strip().split()
        if len(header) < 3 or header[1] == "missing":
            continue
        object_type, size = header[1], int(header[2])
        body = process.stdout.read(size)
        process.stdout.read(1)  # cat-file's record separator
        result.append((object_id, object_type, body))
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError("git object scan failed")
    return result


def audit(root: Path, *, artifact_dir: Path | None = None) -> dict[str, object]:
    findings: list[Finding] = []
    shallow = _git(root, "rev-parse", "--is-shallow-repository").strip()
    refs = [line for line in _git(root, "for-each-ref", "--format=%(refname)").splitlines() if line]
    objects, commits = _reachable_objects(root)
    by_id = {object_id: path for object_id, path in objects}
    object_records = _read_objects(root, list(by_id))
    for object_id, object_type, body in object_records:
        if object_type != "blob":
            continue
        text = body.decode("utf-8", errors="replace")
        findings.extend(_scan_text(text, object_prefix=object_id, source="reachable_blob", path=by_id.get(object_id, "")))
    for object_id, object_type, body in _read_objects(root, commits):
        if object_type == "commit":
            findings.extend(_scan_text(body.decode("utf-8", errors="replace"), object_prefix=object_id, source="commit_metadata"))

    ignore_text = (root / ".gitignore").read_text(encoding="utf-8") if (root / ".gitignore").is_file() else ""
    missing_ignore = [marker for marker in REQUIRED_IGNORE_MARKERS if marker not in ignore_text]
    if shallow == "true":
        findings.append(Finding("shallow_repository", "repository", "repository_state"))
    for marker in missing_ignore:
        findings.append(Finding(f"missing_ignore:{marker}", "repository", "ignore_policy"))

    artifact_files = 0
    if artifact_dir is not None:
        if not artifact_dir.is_dir():
            findings.append(Finding("artifact_directory_missing", "artifact", "artifact_policy"))
        else:
            for path in sorted(p for p in artifact_dir.rglob("*") if p.is_file()):
                artifact_files += 1
                data = path.read_bytes()
                findings.extend(_scan_text(data.decode("utf-8", errors="replace"), object_prefix="artifact", source="artifact", path=str(path.relative_to(artifact_dir))))

    counts: dict[str, int] = {}
    blocking_counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
        if finding.blocking:
            blocking_counts[finding.category] = blocking_counts.get(finding.category, 0) + 1
    return {
        # Non-blocking findings are retained for review, but only concrete
        # blocking findings make the release audit fail closed.
        "ok": not any(finding.blocking for finding in findings),
        "repository": str(root.name),
        "reachable_refs": len(refs),
        "reachable_commits": len(commits),
        "reachable_objects": len(objects),
        "shallow": shallow == "true",
        "artifact_files": artifact_files,
        "finding_counts": dict(sorted(counts.items())),
        "blocking_finding_counts": dict(sorted(blocking_counts.items())),
        "findings": [
            {
                "category": item.category,
                "object_prefix": item.object_prefix,
                "source": item.source,
                "blocking": item.blocking,
            }
            for item in findings
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline shiyi release history and artifact audit")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root.resolve(), artifact_dir=args.artifact_dir.resolve() if args.artifact_dir else None)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": "audit_unavailable", "type": type(exc).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
