from __future__ import annotations

import ingest
import ingest_discord
import query


def test_ingest_fake_provider_makes_no_network_request(monkeypatch):
    monkeypatch.setattr(ingest, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(ingest, "EMBED_DIM", 8)
    embeddings, failed = ingest.embed_texts_with_retry(["alpha", "beta"])
    assert failed == []
    assert len(embeddings) == 2
    assert all(len(vector) == 8 for vector in embeddings)


def test_discord_fake_provider_makes_no_network_request(monkeypatch):
    monkeypatch.setattr(ingest_discord, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(ingest_discord, "EMBED_DIM", 8)
    embeddings, failed = ingest_discord.embed_texts_with_retry(["alpha"])
    assert failed == []
    assert len(embeddings) == 1
    assert len(embeddings[0]) == 8


def test_query_fake_provider_makes_no_network_request(monkeypatch):
    monkeypatch.setattr(query, "EMBEDDING_PROVIDER", "fake")
    monkeypatch.setattr(query, "EMBED_DIM", 8)
    vector = query.embed_query("alpha")
    assert len(vector) == 8
    assert vector == query.embed_query("alpha")
