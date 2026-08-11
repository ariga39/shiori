"""Local-only helper: ingest the task #11 corpus + generated vectors into an
isolated PostgreSQL database for the Phase 4D smoke.

This is a local-only development helper. It uses the vectors already generated
by benchmark/generate_vectors.py into benchmark/.generated (ignored, never
committed) with the pinned voyage-4-nano revision
67fabc9bef010dabc5f6024aa1b1b6b93410426f (offline, no API key, no network).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.product_eval.identity import MODEL_IDENTITY  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest task #11 corpus vectors into isolated PG")
    parser.add_argument("--dsn", required=True, help="isolated PostgreSQL DSN")
    parser.add_argument("--corpus", required=True, type=Path, help="benchmark/fixtures/corpus.jsonl")
    parser.add_argument("--vectors", required=True, type=Path, help="benchmark/.generated/vectors.json")
    parser.add_argument("--session", default="phase4d-smoke", help="session_id prefix for ingested rows")
    args = parser.parse_args(argv)

    docs: dict[str, dict] = {}
    with args.corpus.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                docs[doc["id"]] = doc

    vectors = json.loads(args.vectors.read_text(encoding="utf-8"))
    doc_emb = {v["id"]: v["embedding"] for v in vectors["documents"]}
    missing = set(docs) - set(doc_emb)
    if missing:
        raise SystemExit(f"missing vectors for docs: {sorted(missing)}")

    conn = psycopg2.connect(args.dsn)
    cur = conn.cursor()
    id_map: dict[str, str] = {}
    for doc_id, doc in docs.items():
        emb = str(doc_emb[doc_id])
        ts = datetime.fromisoformat(doc["timestamp"].replace("Z", "+00:00"))
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)
               RETURNING id""",
            (
                f"{args.session}-{doc['session']}",
                doc["source_kind"],
                doc["content"],
                emb,
                MODEL_IDENTITY,
                ts,
                ts,
                0,
                0,
                doc["content"],
                datetime.now(UTC),
            ),
        )
        fetched = cur.fetchone()
        if fetched is None:
            raise SystemExit(f"insert for doc {doc_id} returned no id")
        row_id = fetched[0]
        id_map[str(row_id)] = doc_id
    conn.commit()
    cur.close()
    conn.close()
    map_path = args.vectors.parent / "doc_id_map.json"
    map_path.write_text(json.dumps(id_map, indent=1, sort_keys=True), encoding="utf-8")
    print(f"ingested {len(docs)} docs into session prefix {args.session}; id map -> {map_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
