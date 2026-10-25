"""Load GAMES and ROSTERS for a season from nflverse (idempotent upserts).

Run:  python -m pipelines.load_nflverse --season 2026
"""
from __future__ import annotations

import argparse

from common.log import get_logger
from sources import nflverse

log = get_logger("load_nflverse")


def run(store, season: int) -> tuple[int, int]:
    games = nflverse.schedules_to_games(nflverse.load_schedules(season))
    n_games = store.upsert_games(games)
    rosters = nflverse.rosters_to_rows(nflverse.load_rosters_weekly(season))
    n_rost = store.upsert_rosters(rosters)
    log.info("season %d: %d games, %d roster rows", season, n_games, n_rost)
    return n_games, n_rost


def main() -> None:
    import nflreadpy
    from store import get_store
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=nflreadpy.get_current_season())
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    run(store, args.season)


if __name__ == "__main__":
    main()
