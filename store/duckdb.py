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
