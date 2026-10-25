"""Local replacements for Snowflake Cortex, for when the trial ends (AI_BACKEND=local).

  embeddings   sentence-transformers (LOCAL_EMBED_MODEL, default all-mpnet-base-v2:
               768 dimensions, the same width as snowflake-arctic-embed-m, so the
               VECTOR columns don't change; vectors are normalized)
  completions  Ollama's HTTP API (OLLAMA_URL, OLLAMA_MODEL), temperature 0; structured
               output by passing the JSON schema as Ollama's `format`

Same interface the pipelines use with SnowflakeStore: embed_texts, similarities,
complete, complete_json, suspend_warehouse, close.
"""
from __future__ import annotations

import json
from functools import lru_cache

import httpx

from .settings import env, env_float


@lru_cache(maxsize=1)
def _embedder(name: str):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


class LocalAI:
    # Cosine similarities from all-mpnet-base-v2 spread wider than arctic-embed's:
    # unrelated text sits near 0.1, closely related text around 0.7.
    sim_range = (0.15, 0.75)

    def __init__(self, embed_model: str | None = None, llm_model: str | None = None, url: str | None = None):
        self.embed_model = embed_model or env("LOCAL_EMBED_MODEL", "sentence-transformers/all-mpnet-base-v2")
        self.llm_model = llm_model or env("OLLAMA_MODEL", "qwen2.5:3b")
        self.url = (url or env("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.http = httpx.Client(timeout=env_float("OLLAMA_TIMEOUT_SECONDS", 300))

    # ---- embeddings ------------------------------------------------------------------
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = _embedder(self.embed_model).encode([t or "" for t in texts], normalize_embeddings=True,
                                                  batch_size=32, show_progress_bar=False)
        return [v.tolist() for v in vecs]

    def similarities(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        q, *rest = self.embed_texts([query] + list(texts))
        return [float(sum(a * b for a, b in zip(q, v))) for v in rest]   # normalized: dot = cosine

    # ---- completions -----------------------------------------------------------------
    def _generate(self, prompt: str, temperature: float, fmt=None) -> str:
        body = {"model": self.llm_model, "prompt": prompt, "stream": False,
                "options": {"temperature": temperature, "num_ctx": 8192}}
        if fmt is not None:
            body["format"] = fmt
        r = self.http.post(f"{self.url}/api/generate", json=body)
        r.raise_for_status()
        return r.json().get("response", "")

    def complete(self, prompt: str, temperature: float = 0.0) -> str:
        return self._generate(prompt, temperature)

    def complete_json(self, prompt: str, schema: dict, model: str | None = None) -> dict:
        text = self._generate(prompt, 0.0, fmt=schema)
        try:
            return json.loads(text)
        except ValueError:
            return {}

    def available(self) -> bool:
        try:
            tags = self.http.get(f"{self.url}/api/tags", timeout=5).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return False
        names = {m.get("name") for m in tags} | {m.get("model") for m in tags}
        return self.llm_model in names or f"{self.llm_model}:latest" in names

    # Snowflake-compatible no-ops so callers don't branch.
    def suspend_warehouse(self) -> None:
        pass

    def close(self) -> None:
        self.http.close()
