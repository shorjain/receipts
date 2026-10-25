"""Single entry point: `from store import get_store`. Nothing outside store/ writes SQL."""
from __future__ import annotations

from common.settings import ROOT, env

from .base import SqlStore


def get_store(backend: str | None = None) -> SqlStore:
    backend = (backend or env("STORE_BACKEND", "duckdb")).lower()
    if backend == "snowflake":
        from .snowflake import SnowflakeStore
        return SnowflakeStore()
    if backend == "duckdb":
        from .duckdb import DuckDBStore
        path = env("DUCKDB_PATH", "data/nfl_lines.duckdb")
        return DuckDBStore(path if path == ":memory:" or path.startswith("/") else ROOT / path)
    raise ValueError(f"unknown STORE_BACKEND: {backend}")


__all__ = ["get_store", "SqlStore"]
