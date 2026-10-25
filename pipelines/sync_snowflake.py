"""Copy rows written to the local DuckDB store since the last sync into Snowflake.

Minute-by-minute polling stays local (a warehouse woken every minute would never
suspend: ~24 credits a day). Once an hour this job:
  1. reads each table's rows with updated_at in (last sync, now],
  2. upserts them into Snowflake in chunks (embedding columns are skipped:
     Snowflake owns those),
  3. fills missing POSTS / LINE_MOVES embeddings server-side with AI_EMBED,
  4. suspends the warehouse immediately instead of idling AUTO_SUSPEND seconds.

Run:  python -m pipelines.sync_snowflake          (also runs inside ingest_odds --loop)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from common.log import get_logger
from store.schema import LOCAL_ONLY, TABLES, VEC

log = get_logger("sync_snowflake")

CHUNK = 20_000
EMBED_TARGETS = {"POSTS": "text", "LINE_MOVES": "summary_text"}


def sync(local, remote, now: datetime | None = None, chunk: int = CHUNK) -> dict[str, int]:
    """Upsert changed rows from `local` into `remote` (any two SqlStores)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    state = local.sync_state()
    counts: dict[str, int] = {}
    for table, (cols, _) in TABLES.items():
        if table in LOCAL_ONLY:
            continue
        since = state.get(table, {}).get("synced_through")
        vec_cols = {c for c, t in cols if t == VEC}
        rows = [{k: v for k, v in r.items() if k not in vec_cols}
                for r in local.rows_changed(table, since, now)]
        for i in range(0, len(rows), chunk):
            remote.upsert(table, rows[i:i + chunk])
        local.set_sync_state(table, now, len(rows))
        if rows:
            counts[table] = len(rows)
    return counts


def run(local) -> dict[str, int]:
    from store.snowflake import SnowflakeStore
    started = time.time()
    remote = SnowflakeStore()
    try:
        remote.create_schema()
        counts = sync(local, remote)
        embedded = {t: remote.embed_missing(t, col) for t, col in EMBED_TARGETS.items()}
        remote.suspend_warehouse()
    finally:
        remote.close()
    log.info("synced to Snowflake in %.0fs: %s; embedded %s", time.time() - started,
             counts or "nothing new", {k: v for k, v in embedded.items() if v} or "nothing")
    return counts


def main() -> None:
    from store import get_store
    local = get_store("duckdb")
    local.create_schema()
    run(local)


if __name__ == "__main__":
    main()
