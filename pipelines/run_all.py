"""Run every implemented stage once, in order. Safe to re-run (all writes are upserts).

Run:  python -m pipelines.run_all [--season 2026] [--skip-nflverse]
"""
from __future__ import annotations

import argparse

from pipelines import compare_venues, detect_moves, ingest_odds, load_nflverse


def main() -> None:
    import nflreadpy
    from store import get_store
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=nflreadpy.get_current_season())
    ap.add_argument("--skip-nflverse", action="store_true", help="games/rosters already loaded today")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    if not args.skip_nflverse:
        load_nflverse.run(store, args.season)
    ingest_odds.run_once(store)
    detect_moves.run(store)
    compare_venues.run(store)


if __name__ == "__main__":
    main()
