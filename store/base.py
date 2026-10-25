"""The store interface. Pipelines call these functions; only store/ writes SQL.

SqlStore holds the queries that are portable between Snowflake and DuckDB (qmark
params, MERGE, no dialect-specific date functions: times are computed in Python).
Subclasses supply the connection, DDL type mapping and AI functions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .schema import EMBED_DIM, JSON, TABLES, VEC


def utc_naive(ts: datetime | None) -> datetime | None:
    """Convert to naive UTC, the storage convention for every TS column."""
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _bind(value: Any, col_type: str) -> Any:
    if value is None:
        return None
    if col_type in (JSON, VEC):
        return value if isinstance(value, str) else json.dumps(value, default=str)
    if isinstance(value, datetime):
        return utc_naive(value)
    return value


class SqlStore:
    dialect = "base"
    TYPE_MAP: dict[str, str] = {}

    # ---- subclass hooks ----------------------------------------------------
    def _execute(self, sql: str, params: Sequence | None = None) -> None:
        raise NotImplementedError

    def _executemany(self, sql: str, rows: list[Sequence]) -> None:
        raise NotImplementedError

    def _query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        raise NotImplementedError

    def _cast_from_text(self, expr: str, col_type: str) -> str:
        """SQL that turns a text column holding JSON into the target type."""
        raise NotImplementedError

    # ---- schema ------------------------------------------------------------
    def create_schema(self) -> None:
        for table, (cols, pk) in TABLES.items():
            col_sql = ", ".join(f"{name} {self.TYPE_MAP[t]}" for name, t in cols)
            self._execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({col_sql}, PRIMARY KEY ({', '.join(pk)}))"
            )

    # ---- generic upsert ----------------------------------------------------
    def upsert(self, table: str, rows: Iterable[dict]) -> int:
        """Idempotent insert-or-update keyed on the table's primary key."""
        rows = list(rows)
        if not rows:
            return 0
        cols, pk = TABLES[table]
        names = [c for c, _ in cols]
        types = dict(cols)
        present = [c for c in names if any(c in r for r in rows)]
        for k in pk:
            if k not in present:
                raise ValueError(f"{table}: rows missing key column {k}")
        tmp = f"TMP_{table}"
        tmp_cols = ", ".join(
            f"{c} {self.TYPE_MAP['STR'] if types[c] in (JSON, VEC) else self.TYPE_MAP[types[c]]}"
            for c in present
        )
        self._execute(f"CREATE OR REPLACE TEMPORARY TABLE {tmp} ({tmp_cols})")
        # Drop duplicate keys inside the batch (last one wins) so MERGE stays deterministic.
        dedup: dict[tuple, dict] = {}
        for r in rows:
            dedup[tuple(_bind(r.get(k), types[k]) for k in pk)] = r
        placeholders = ", ".join("?" for _ in present)
        self._executemany(
            f"INSERT INTO {tmp} ({', '.join(present)}) VALUES ({placeholders})",
            [[_bind(r.get(c), types[c]) for c in present] for r in dedup.values()],
        )
        src = {c: (self._cast_from_text(f"s.{c}", types[c]) if types[c] in (JSON, VEC) else f"s.{c}")
               for c in present}
        on = " AND ".join(f"t.{k} = s.{k}" for k in pk)
        non_key = [c for c in present if c not in pk]
        update = ", ".join(f"{c} = {src[c]}" for c in non_key)
        merge = f"MERGE INTO {table} t USING {tmp} s ON {on} "
        if update:
            merge += f"WHEN MATCHED THEN UPDATE SET {update} "
        merge += (f"WHEN NOT MATCHED THEN INSERT ({', '.join(present)}) "
                  f"VALUES ({', '.join(src[c] for c in present)})")
        self._execute(merge)
        self._execute(f"DROP TABLE IF EXISTS {tmp}")
        return len(dedup)

    # ---- GAMES / ROSTERS ---------------------------------------------------
    def upsert_games(self, rows: Iterable[dict]) -> int:
        return self.upsert("GAMES", rows)

    def get_games(self, season: int | None = None, week: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM GAMES WHERE 1=1", []
        if season is not None:
            sql += " AND season = ?"; params.append(season)
        if week is not None:
            sql += " AND week = ?"; params.append(week)
        return self._query(sql + " ORDER BY kickoff_at", params)

    def games_between(self, start: datetime, end: datetime) -> list[dict]:
        return self._query(
            "SELECT * FROM GAMES WHERE kickoff_at >= ? AND kickoff_at < ? ORDER BY kickoff_at",
            [utc_naive(start), utc_naive(end)],
        )

    def upsert_rosters(self, rows: Iterable[dict]) -> int:
        return self.upsert("ROSTERS", rows)

    def roster(self, season: int, team: str | None = None, week: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM ROSTERS WHERE season = ?", [season]
        if team:
            sql += " AND team = ?"; params.append(team)
        if week is not None:
            sql += " AND week = ?"; params.append(week)
        return self._query(sql, params)

    # ---- MARKETS / ODDS ----------------------------------------------------
    def upsert_markets(self, rows: Iterable[dict]) -> int:
        return self.upsert("MARKETS", rows)

    def insert_odds_snapshots(self, rows: Iterable[dict]) -> int:
        return self.upsert("ODDS_SNAPSHOTS", rows)

    def insert_odds_snapshot(self, row: dict) -> int:
        return self.insert_odds_snapshots([row])

    def latest_snapshots(self, market_ids: Sequence[str] | None = None) -> dict[str, dict]:
        """Most recent snapshot per market (used to skip unchanged Kalshi quotes)."""
        sql = ("SELECT o.* FROM ODDS_SNAPSHOTS o JOIN (SELECT market_id, MAX(taken_at) AS m "
               "FROM ODDS_SNAPSHOTS GROUP BY market_id) x "
               "ON o.market_id = x.market_id AND o.taken_at = x.m")
        rows = self._query(sql)
        out = {r["market_id"]: r for r in rows}
        if market_ids is not None:
            keep = set(market_ids)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def odds_series(self, start: datetime, end: datetime, game_id: str | None = None,
                    market_type: str | None = None, sides: Sequence[str] | None = None) -> list[dict]:
        """Snapshots joined to their market, ordered by time."""
        sql = ("SELECT m.venue, m.game_id, m.market_type, m.side, o.* FROM ODDS_SNAPSHOTS o "
               "JOIN MARKETS m ON m.market_id = o.market_id "
               "WHERE o.taken_at >= ? AND o.taken_at <= ?")
        params: list = [utc_naive(start), utc_naive(end)]
        if game_id:
            sql += " AND m.game_id = ?"; params.append(game_id)
        if market_type:
            sql += " AND m.market_type = ?"; params.append(market_type)
        if sides:
            sql += f" AND m.side IN ({', '.join('?' for _ in sides)})"; params.extend(sides)
        return self._query(sql + " ORDER BY o.taken_at", params)

    # ---- LINE_MOVES --------------------------------------------------------
    def upsert_line_moves(self, rows: Iterable[dict]) -> int:
        return self.upsert("LINE_MOVES", rows)

    def line_moves(self, start: datetime | None = None, end: datetime | None = None,
                   game_id: str | None = None, explain_status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM LINE_MOVES WHERE 1=1", []
        if start is not None:
            sql += " AND window_end >= ?"; params.append(utc_naive(start))
        if end is not None:
            sql += " AND window_start <= ?"; params.append(utc_naive(end))
        if game_id:
            sql += " AND game_id = ?"; params.append(game_id)
        if explain_status:
            sql += " AND explain_status = ?"; params.append(explain_status)
        return self._query(sql + " ORDER BY window_start DESC", params)

    def explain_status_by_move(self, move_ids: Sequence[str]) -> dict[str, str]:
        if not move_ids:
            return {}
        rows = self._query(
            f"SELECT move_id, explain_status FROM LINE_MOVES WHERE move_id IN "
            f"({', '.join('?' for _ in move_ids)})", list(move_ids))
        return {r["move_id"]: r["explain_status"] for r in rows}

    # ---- API usage ---------------------------------------------------------
    def log_api_usage(self, source: str, endpoint: str, cost_units: float,
                      remaining: float | None, note: str = "") -> None:
        self.upsert("API_USAGE", [{
            "called_at": datetime.now(timezone.utc), "source": source, "endpoint": endpoint,
            "cost_units": cost_units, "remaining": remaining, "note": note,
        }])

    def last_api_usage(self, source: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM API_USAGE WHERE source = ? ORDER BY called_at DESC LIMIT 1", [source])
        return rows[0] if rows else None

    # ---- AI (overridden per backend) --------------------------------------
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def complete(self, prompt: str, temperature: float = 0.0) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


__all__ = ["SqlStore", "utc_naive", "EMBED_DIM"]
