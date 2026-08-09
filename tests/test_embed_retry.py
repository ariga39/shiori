import time

import pytest
import requests

import ingest
import ingest_discord

EMB = [0.1] * 1024


class FakeResp:
    def __init__(self, status, data=None, retry_after=None):
        self.status_code = status
        self._data = data
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def ok_resp(n):
    return FakeResp(200, {"data": [{"embedding": EMB} for _ in range(n)]})


MODULES = [ingest, ingest_discord]


@pytest.fixture
def patch_voyage(monkeypatch):
    """Point the Voyage credential/file + time boundaries at mocks."""
    sleeps = []

    def _noop(secs):
        sleeps.append(secs)

    monkeypatch.setattr(time, "sleep", _noop)
    for mod in MODULES:
        monkeypatch.setattr(mod, "_read_voyage_key", lambda: "test-key")
    yield sleeps


@pytest.mark.parametrize("mod", MODULES)
def test_all_success(monkeypatch, patch_voyage, mod):
    monkeypatch.setattr(requests, "post", lambda *a, **k: ok_resp(2))
    embeddings, failed = mod.embed_texts_with_retry(["a", "b"])
    assert failed == []
    assert embeddings == [EMB, EMB]


@pytest.mark.parametrize("mod", MODULES)
def test_partial_failure_returns_failed_indices(monkeypatch, patch_voyage, mod):
    monkeypatch.setattr(mod, "VOYAGE_BATCH_SIZE", 2)

    def fake_post(url, **kw):
        batch = kw["json"]["input"]
        if len(batch) == 2:  # first batch succeeds
            return ok_resp(2)
        return FakeResp(500)  # second batch keeps failing

    monkeypatch.setattr(requests, "post", fake_post)
    embeddings, failed = mod.embed_texts_with_retry(["a", "b", "c"])
    assert failed == [2]
    assert embeddings[:2] == [EMB, EMB]
    assert embeddings[2] is None


@pytest.mark.parametrize("mod", MODULES)
def test_429_retries_per_retry_after_then_succeeds(monkeypatch, patch_voyage, mod):
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, retry_after=0.25)
        return ok_resp(1)

    monkeypatch.setattr(requests, "post", fake_post)
    embeddings, failed = mod.embed_texts_with_retry(["a"])
    assert failed == []
    assert embeddings == [EMB]
    assert calls["n"] == 2
    assert 0.25 in patch_voyage  # honored Retry-After before retrying


@pytest.mark.parametrize("mod", MODULES)
def test_consistent_failure_returns_failed_indices(monkeypatch, patch_voyage, mod):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(500))
    embeddings, failed = mod.embed_texts_with_retry(["a", "b"])
    assert failed == [0, 1]
    assert embeddings == [None, None]
