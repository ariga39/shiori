"""Deterministic embeddings for explicitly enabled local development only.

The production path remains the Voyage provider.  This module deliberately has
no network or credential access; callers must opt into it through the typed
``allow_fake_embeddings`` setting before using the ``fake`` provider.
"""

from __future__ import annotations

import hashlib
import math


def deterministic_embedding(text: str, *, dimension: int) -> list[float]:
    """Return a stable, bounded vector for local smoke tests.

    The construction is intentionally simple and is not suitable for semantic
    retrieval quality.  Hashing each indexed position avoids a dependency on
    process-global random state and keeps clean-machine tests reproducible.
    """
    if not isinstance(text, str):
        raise TypeError("fake embedding input must be text")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        raise ValueError("fake embedding dimension must be positive")

    values: list[float] = []
    for index in range(dimension):
        digest = hashlib.sha256(f"shiori-fake-v1:{index}:".encode() + text.encode("utf-8")).digest()
        raw = int.from_bytes(digest[:8], "big") / float(1 << 64)
        values.append((raw * 2.0) - 1.0)
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return [0.0] * dimension
    return [value / norm for value in values]
