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


def snapshot_path():
    return ROOT / env("UI_SNAPSHOT_PATH", "data/ui_snapshot.duckdb")


def get_read_store() -> SqlStore:
    """Read-only store for the UI and API: the loop's latest snapshot when it exists,
    else the main DuckDB file (only works while the loop isn't running)."""
    from .duckdb import DuckDBStore
    snap = snapshot_path()
    if snap.exists():
        return DuckDBStore(snap, read_only=True)
    path = env("DUCKDB_PATH", "data/nfl_lines.duckdb")
    return DuckDBStore(path if path.startswith("/") else ROOT / path, read_only=True)


__all__ = ["get_store", "get_read_store", "snapshot_path", "SqlStore"]
