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

    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))

    def _execute(self, sql: str, params: Sequence | None = None) -> None:
        self.con.execute(sql, params or [])

    def _executemany(self, sql: str, rows: list[Sequence]) -> None:
        self.con.executemany(sql, rows)

    def _query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _cast_from_text(self, expr: str, col_type: str) -> str:
        if col_type == VEC:
            return f"CAST({expr} AS FLOAT[{EMBED_DIM}])"
        if col_type == JSON:
            return f"CAST({expr} AS JSON)"
        return expr

    def close(self) -> None:
        self.con.close()
