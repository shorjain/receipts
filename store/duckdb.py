"""DuckDB implementation of the store. Used for tests and local dev now, and as the
post-trial fallback later (milestone 10 adds local embeddings and an Ollama LLM)."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import duckdb

from .base import SqlStore
from .schema import EMBED_DIM, JSON, VEC


class DuckDBStore(SqlStore):
    dialect = "duckdb"
    TYPE_MAP = {
        "STR": "VARCHAR", "INT": "BIGINT", "FLOAT": "DOUBLE", "BOOL": "BOOLEAN",
        "TS": "TIMESTAMP", "JSON": "JSON", "VEC": f"FLOAT[{EMBED_DIM}]",
    }

    def __init__(self, path: str | Path = ":memory:", read_only: bool = False):
        if path != ":memory:" and not read_only:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path), read_only=read_only)

    def _execute(self, sql: str, params: Sequence | None = None) -> None:
        self.con.execute(sql, params or [])

    def _executemany(self, sql: str, rows: list[Sequence]) -> None:
        self.con.executemany(sql, rows)

    def _query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ---- local vector search (AI_BACKEND=local) -----------------------------------
    def _local_ai(self):
        from pipelines.explain_moves import ai_backend
        if ai_backend() != "local":
            return None
        if not hasattr(self, "_ai"):
            from common.local_ai import LocalAI
            self._ai = LocalAI()
        return self._ai

    def _similarity_sql(self, query: str | None) -> tuple[str, list] | None:
        ai = self._local_ai() if query else None
        if ai is None:
            return None
        vec = ai.embed_texts([query])[0]
        return f"array_cosine_similarity(embedding, CAST(? AS FLOAT[{EMBED_DIM}]))", [vec]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        from common.local_ai import LocalAI
        return (self._local_ai() or LocalAI()).embed_texts(texts)

    def embed_missing(self, table: str, text_col: str, limit: int = 500) -> int:
        """Fill NULL embeddings locally (posts, move summaries) when Snowflake isn't used."""
        key = {"POSTS": "post_id", "LINE_MOVES": "move_id"}[table]
        rows = self._query(f"SELECT {key}, {text_col} AS t FROM {table} WHERE embedding IS NULL "
                           f"AND {text_col} IS NOT NULL AND {text_col} <> '' LIMIT ?", [limit])
        if not rows:
            return 0
        vecs = self.embed_texts([r["t"] for r in rows])
        self.upsert(table, [{key: r[key], "embedding": v} for r, v in zip(rows, vecs)])
        return len(rows)

    def _cast_from_text(self, expr: str, col_type: str) -> str:
        if col_type == VEC:
            return f"CAST({expr} AS FLOAT[{EMBED_DIM}])"
        if col_type == JSON:
            return f"CAST({expr} AS JSON)"
        return expr

    def snapshot(self, dest: str | Path) -> Path:
        """Copy the whole database to `dest` atomically. DuckDB lets only one process
        open a file for writing, so the UI and API read this copy instead."""
        dest = Path(dest)
        tmp = dest.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        db = self.con.execute("SELECT current_database()").fetchone()[0]
        self.con.execute(f"ATTACH '{tmp}' AS ui_snap")
        try:
            self.con.execute(f"COPY FROM DATABASE {db} TO ui_snap")
        finally:
            self.con.execute("DETACH ui_snap")
        tmp.replace(dest)
        return dest

    def close(self) -> None:
        self.con.close()
