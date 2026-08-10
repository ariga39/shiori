"""Validate that benchmark fixtures are rebuildable from fixed inputs.

Recomputes hashes over the corpus/query files and (optionally) generated
vectors, comparing against the committed manifest.  Fails closed on any
mismatch so a mutated fixture cannot silently invalidate a baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate benchmark fixture hashes")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--document-vectors", type=Path)
    parser.add_argument("--query-vectors", type=Path)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = manifest.get("hashes", {})
    failures: list[str] = []

    checks = [
        ("documents", args.documents),
        ("queries", args.queries),
        ("document_vectors", args.document_vectors),
        ("query_vectors", args.query_vectors),
    ]
    for key, path in checks:
        if key in expected:
            if path is None or not path.exists():
                failures.append(f"{key}: missing path {path}")
                continue
            if key in ("documents", "queries"):
                actual = _sha256_file(path)
            else:
                actual = _sha256_text(path.read_text(encoding="utf-8"))
            if actual != expected[key]:
                failures.append(f"{key}: hash mismatch expected={expected[key][:12]} got={actual[:12]}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("OK: all fixture hashes match manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
