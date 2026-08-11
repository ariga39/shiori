"""Local-only generator for the Phase 4D 72-dev query vectors (task #18).

Reads the frozen dataset_manifest.json and selects EXACTLY the 72 development
(tune) query ids. Any holdout / extra / missing / duplicate id is rejected
(fail closed). Emits offline query embeddings using the pinned
voyageai/voyage-4-nano revision from benchmark.product_eval.identity with
encode_query, truncate_dim=1024, float32, L2 normalization.

LOCAL-ONLY by contract: downloads/loads the pinned model from the local cache
(never in CI), writes to an ignored output directory, never commits vectors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.product_eval.identity import EMBED_DIM, MODEL_ID, MODEL_REVISION  # noqa: E402
from benchmark.product_eval.manifest import load_golden_rows, load_manifest  # noqa: E402


def select_dev_ids(manifest_path: Path, rows_path: Path) -> list[dict]:
    """Return the golden rows whose query_id is in the 72-dev tune split.

    Fails closed if the manifest split is not exactly 72 tune / 48 holdout,
    if golden rows contain duplicate query ids, if the golden id set does not
    equal tune+holdout, or if any selected row's id is not a dev id.
    """
    manifest = load_manifest(manifest_path)
    dev = {s["query_id"] for s in manifest["query_splits"] if s["split"] == "tune"}
    holdout = {s["query_id"] for s in manifest["query_splits"] if s["split"] == "holdout"}
    if len(dev) != 72 or len(holdout) != 48:
        raise SystemExit(f"split must be exactly 72/48, got {len(dev)}/{len(holdout)}")
    if dev & holdout:
        raise SystemExit("tune/holdout overlap")
    rows = load_golden_rows(rows_path)
    if len(rows) != 120 or len({r["query_id"] for r in rows}) != 120:
        raise SystemExit("golden rows must contain exactly 120 unique query ids")
    by_id = {r["query_id"]: r for r in rows}
    if set(by_id) != (dev | holdout):
        missing = sorted((dev | holdout) - set(by_id))
        extra = sorted(set(by_id) - (dev | holdout))
        raise SystemExit(f"golden rows id set must equal tune+holdout: missing={missing} extra={extra}")
    return [by_id[q] for q in sorted(dev)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate local 72-dev query vectors (voyage-4-nano)")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rows", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="output dev_query_vectors.json")
    args = parser.parse_args(argv)

    dev_rows = select_dev_ids(args.manifest, args.rows)
    if len(dev_rows) != 72:
        raise SystemExit(f"expected 72 dev rows, got {len(dev_rows)}")

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        MODEL_ID,
        revision=MODEL_REVISION,
        trust_remote_code=True,
        truncate_dim=EMBED_DIM,
        device="cpu",
    )
    from benchmark.query_rendering import render_canonical_query  # noqa: E402

    out = []
    for row in dev_rows:
        qtext = render_canonical_query(row)
        emb = model.encode_query(
            qtext, truncate_dim=EMBED_DIM, precision="float32",
            normalize_embeddings=True, show_progress_bar=False,
        ).tolist()
        out.append({"query_id": row["query_id"], "embedding": emb})

    args.out.write_text(json.dumps(out), encoding="utf-8")
    print(f"wrote {args.out.name} with {len(out)} dev query vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
