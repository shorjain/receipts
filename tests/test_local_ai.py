import hashlib
import json
from datetime import datetime, timedelta

import httpx
import numpy as np
import pytest

from common import local_ai
from pipelines import explain_moves
from store.schema import EMBED_DIM


class FakeEmbedder:
    """Bag-of-words vectors: texts sharing words are similar, like a real embedder."""

    def encode(self, texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False):
        out = []
        for t in texts:
            v = np.zeros(EMBED_DIM)
            for w in t.lower().split():
                v[int(hashlib.md5(w.strip(".,").encode()).hexdigest(), 16) % EMBED_DIM] += 1
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return np.array(out)


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("AI_BACKEND", "local")
    monkeypatch.setattr(local_ai, "_embedder", lambda name: FakeEmbedder())
    seen = []

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
        body = json.loads(request.content)
        seen.append(body)
        reply = json.dumps({"claims": []}) if body.get("format") else "Lamar Jackson is out [P1]."
        return httpx.Response(200, json={"response": reply})

    real_client = httpx.Client
    monkeypatch.setattr(local_ai.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return seen


def test_backend_choice(monkeypatch):
    monkeypatch.setenv("AI_BACKEND", "local")
    assert explain_moves.ai_backend() == "local"
    monkeypatch.setenv("AI_BACKEND", "snowflake")
    assert explain_moves.ai_backend() == "snowflake"
    monkeypatch.delenv("AI_BACKEND")
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.setattr("common.settings.env", lambda k, d=None: d)
    assert explain_moves.ai_backend() == "local"            # no Snowflake account -> local


def test_local_completion_and_structured_output(local):
    ai = local_ai.LocalAI()
    assert ai.available()
    assert ai.complete("q", temperature=0) == "Lamar Jackson is out [P1]."
    assert local[-1]["options"]["temperature"] == 0 and local[-1]["model"] == "qwen2.5:3b"
    assert ai.complete_json("q", {"type": "object"}) == {"claims": []}
    assert local[-1]["format"] == {"type": "object"}        # schema passed as Ollama's format


def test_local_similarities_rank_related_text_higher(local):
    s = local_ai.LocalAI().similarities("ravens lamar jackson out", ["lamar jackson ruled out ravens", "bills sign punter"])
    assert s[0] > s[1]


def test_get_ai_returns_none_when_ollama_is_down(monkeypatch):
    monkeypatch.setenv("AI_BACKEND", "local")
    monkeypatch.setattr(local_ai.LocalAI, "available", lambda self: False)
    assert explain_moves.get_ai() is None


def test_duckdb_vector_search_and_backfill(local, store):
    t0 = datetime(2026, 10, 24, 19)
    store.upsert_posts([
        {"post_id": "1", "author": "a", "posted_at": t0, "text": "Ravens rule out Lamar Jackson ankle", "url": "u1"},
        {"post_id": "2", "author": "b", "posted_at": t0 + timedelta(minutes=5), "text": "Bills sign a punter", "url": "u2"},
        {"post_id": "3", "author": "c", "posted_at": t0 + timedelta(minutes=9), "text": "Great day for football", "url": "u3"}])
    assert store.embed_missing("POSTS", "text") == 3
    assert store.embed_missing("POSTS", "text") == 0          # idempotent
    top = store.ask_posts(t0 - timedelta(hours=1), t0 + timedelta(hours=1), query="Lamar Jackson out for Ravens", limit=3)
    assert top[0]["post_id"] == "1"                           # semantic, not newest-first


def test_explain_uses_the_backend_similarity_range():
    assert explain_moves.relevance(0.45, False, local_ai.LocalAI.sim_range) == pytest.approx(0.5)
    assert explain_moves.relevance(0.45, False) == 0.0        # Snowflake range would call it unrelated
