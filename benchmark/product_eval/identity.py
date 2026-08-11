"""Single shared frozen embedding identity for Phase 4D (task #18).

runner.py, tools/phase4d_ingest_corpus.py, and the SQL model-identity gate all
read from this one module so the identity cannot drift between three places.
The full identity (model + pinned revision) is the ONLY value written to the
DB embedding_model column and used for filtering.
"""

MODEL_ID = "voyageai/voyage-4-nano"
MODEL_REVISION = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
MODEL_IDENTITY = f"{MODEL_ID}@{MODEL_REVISION}"
EMBED_DIM = 1024
