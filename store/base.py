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
        """Create missing tables, then add any columns that schema.py gained since."""
        for table, (cols, pk) in TABLES.items():
            col_sql = ", ".join(f"{name} {self.TYPE_MAP[t]}" for name, t in cols)
            self._execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({col_sql}, PRIMARY KEY ({', '.join(pk)}))"
            )
        existing: dict[str, set[str]] = {}
        for r in self._query("SELECT table_name, column_name FROM information_schema.columns "
                             "WHERE table_schema = CURRENT_SCHEMA()"):
            existing.setdefault(r["table_name"].upper(), set()).add(r["column_name"].lower())
        now = utc_naive(datetime.now(timezone.utc))
        for table, (cols, _) in TABLES.items():
            have = existing.get(table, set())
            for name, t in cols:
                if have and name not in have:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {self.TYPE_MAP[t]}")
                    if name == "updated_at":  # existing rows count as written now
                        self._execute(f"UPDATE {table} SET updated_at = ? WHERE updated_at IS NULL", [now])

    # ---- generic upsert ----------------------------------------------------
    def upsert(self, table: str, rows: Iterable[dict]) -> int:
        """Idempotent insert-or-update keyed on the table's primary key."""
        rows = list(rows)
        if not rows:
            return 0
        now = utc_naive(datetime.now(timezone.utc))
        rows = [r if r.get("updated_at") else {**r, "updated_at": now} for r in rows]
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

    # ---- change tracking (used by the Snowflake sync) -----------------------
    def rows_changed(self, table: str, since: datetime | None, until: datetime) -> list[dict]:
        sql, params = f"SELECT * FROM {table} WHERE updated_at <= ?", [utc_naive(until)]
        if since is not None:
            sql += " AND updated_at > ?"; params.append(utc_naive(since))
        return self._query(sql, params)

    def sync_state(self) -> dict[str, dict]:
        return {r["table_name"]: r for r in self._query("SELECT * FROM SYNC_STATE")}

    def set_sync_state(self, table: str, synced_through: datetime, rows_synced: int) -> None:
        self.upsert("SYNC_STATE", [{"table_name": table, "synced_through": synced_through,
                                    "rows_synced": rows_synced}])

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

    # ---- POSTS / SEARCH_RUNS / MOVE_POSTS ----------------------------------
    def upsert_posts(self, rows: Iterable[dict]) -> int:
        return self.upsert("POSTS", rows)

    def posts_by_id(self, post_ids: Sequence[str]) -> dict[str, dict]:
        if not post_ids:
            return {}
        rows = self._query(f"SELECT * FROM POSTS WHERE post_id IN ({', '.join('?' for _ in post_ids)})",
                           list(post_ids))
        return {r["post_id"]: r for r in rows}

    def posts_in_window(self, start: datetime, end: datetime,
                        authors: Sequence[str] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM POSTS WHERE posted_at >= ? AND posted_at <= ?", [utc_naive(start), utc_naive(end)]
        if authors:
            sql += f" AND author IN ({', '.join('?' for _ in authors)})"; params.extend(authors)
        return self._query(sql + " ORDER BY posted_at", params)

    def insert_search_run(self, row: dict) -> None:
        self.upsert("SEARCH_RUNS", [row])

    def search_cost_since(self, since: datetime, purposes: Sequence[str] | None = None) -> float:
        sql, params = "SELECT SUM(cost_estimate_usd) AS c FROM SEARCH_RUNS WHERE ran_at >= ?", [utc_naive(since)]
        if purposes:
            sql += f" AND purpose IN ({', '.join('?' for _ in purposes)})"; params.extend(purposes)
        rows = self._query(sql, params)
        return float(rows[0]["c"] or 0) if rows else 0.0

    def search_runs(self, purpose: str | None = None, since: datetime | None = None) -> list[dict]:
        sql, params = "SELECT * FROM SEARCH_RUNS WHERE 1=1", []
        if purpose:
            sql += " AND purpose = ?"; params.append(purpose)
        if since is not None:
            sql += " AND ran_at >= ?"; params.append(utc_naive(since))
        return self._query(sql + " ORDER BY ran_at DESC", params)

    def upsert_move_posts(self, rows: Iterable[dict]) -> int:
        return self.upsert("MOVE_POSTS", rows)

    def move_posts(self, move_id: str) -> list[dict]:
        return self._query("SELECT mp.*, p.author, p.posted_at, p.text, p.url FROM MOVE_POSTS mp "
                           "JOIN POSTS p ON p.post_id = mp.post_id WHERE mp.move_id = ? "
                           "ORDER BY p.posted_at", [move_id])

    def insider_credibility(self, author: str) -> float | None:
        rows = self._query("SELECT credibility_weight FROM INSIDER_SCORES WHERE author = ? "
                           "AND score_window = 'season' ORDER BY season DESC LIMIT 1", [author])
        return float(rows[0]["credibility_weight"]) if rows and rows[0]["credibility_weight"] is not None else None

    # ---- CLAIMS / RESOLUTIONS / SCORES ------------------------------------
    def posts_needing_claims(self, limit: int = 500) -> list[dict]:
        return self._query("SELECT * FROM POSTS WHERE claims_checked_at IS NULL AND text IS NOT NULL "
                           "ORDER BY posted_at LIMIT ?", [limit])

    def mark_claims_checked(self, post_ids: Sequence[str], when: datetime) -> None:
        self.upsert("POSTS", [{"post_id": p, "claims_checked_at": when} for p in post_ids])

    def upsert_claims(self, rows: Iterable[dict]) -> int:
        return self.upsert("CLAIMS", rows)

    def claims(self, since: datetime | None = None, author: str | None = None) -> list[dict]:
        """Claims joined with their resolution and post URL."""
        sql = ("SELECT c.*, r.outcome, r.evidence, r.resolved_at, p.url, p.text AS post_text "
               "FROM CLAIMS c LEFT JOIN CLAIM_RESOLUTIONS r ON r.claim_id = c.claim_id "
               "LEFT JOIN POSTS p ON p.post_id = c.post_id WHERE 1=1")
        params: list = []
        if since is not None:
            sql += " AND c.posted_at >= ?"; params.append(utc_naive(since))
        if author:
            sql += " AND c.author = ?"; params.append(author)
        return self._query(sql + " ORDER BY c.posted_at", params)

    def upsert_resolutions(self, rows: Iterable[dict]) -> int:
        return self.upsert("CLAIM_RESOLUTIONS", rows)

    def upsert_insider_scores(self, rows: Iterable[dict]) -> int:
        return self.upsert("INSIDER_SCORES", rows)

    def insider_scores(self, season: int | None = None, score_window: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM INSIDER_SCORES WHERE 1=1", []
        if season is not None:
            sql += " AND season = ?"; params.append(season)
        if score_window:
            sql += " AND score_window = ?"; params.append(score_window)
        return self._query(sql + " ORDER BY credibility_weight DESC", params)

    def delete_moves(self, move_ids: Sequence[str]) -> int:
        """Remove moves and their post links (local only: the Snowflake sync is upsert-only)."""
        ids = list(move_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ", ".join("?" for _ in chunk)
            self._execute(f"DELETE FROM MOVE_POSTS WHERE move_id IN ({marks})", chunk)
            self._execute(f"DELETE FROM LINE_MOVES WHERE move_id IN ({marks})", chunk)
        return len(ids)

    def relabel_snapshots(self, market_id: str, before: datetime, source_from: str, source_to: str) -> None:
        self._execute("UPDATE ODDS_SNAPSHOTS SET source = ?, updated_at = ? WHERE market_id = ? "
                      "AND taken_at < ? AND source = ?",
                      [source_to, utc_naive(datetime.now(timezone.utc)), market_id, utc_naive(before), source_from])

    # ---- BACKFILL_STATE ----------------------------------------------------
    def backfill_tasks(self, status: str | None = None, kind: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM BACKFILL_STATE WHERE 1=1", []
        if status:
            sql += " AND status = ?"; params.append(status)
        if kind:
            sql += " AND kind = ?"; params.append(kind)
        return self._query(sql + " ORDER BY priority DESC, task_id", params)

    def upsert_backfill_tasks(self, rows: Iterable[dict]) -> int:
        return self.upsert("BACKFILL_STATE", rows)

    # ---- VENUE_GAPS --------------------------------------------------------
    def upsert_venue_gaps(self, rows: Iterable[dict]) -> int:
        return self.upsert("VENUE_GAPS", rows)

    def venue_gaps(self, start: datetime | None = None, end: datetime | None = None,
                   game_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM VENUE_GAPS WHERE 1=1", []
        if start is not None:
            sql += " AND gap_end >= ?"; params.append(utc_naive(start))
        if end is not None:
            sql += " AND gap_start <= ?"; params.append(utc_naive(end))
        if game_id:
            sql += " AND game_id = ?"; params.append(game_id)
        return self._query(sql + " ORDER BY gap_start DESC", params)

    # ---- API usage ---------------------------------------------------------
    def log_api_usage(self, source: str, endpoint: str, cost_units: float,
                      remaining: float | None, note: str = "") -> None:
        self.upsert("API_USAGE", [{
            "called_at": datetime.now(timezone.utc), "source": source, "endpoint": endpoint,
            "cost_units": cost_units, "remaining": remaining, "note": note,
        }])

    def api_calls_since(self, source: str, since: datetime) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM API_USAGE WHERE source = ? AND called_at >= ?",
                           [source, utc_naive(since)])
        return int(rows[0]["n"]) if rows else 0

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
