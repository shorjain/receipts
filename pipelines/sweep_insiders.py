"""Scheduled sweeps of tracked insiders, for claim extraction (milestone 6).

Schedule (brief): every 15 minutes from 90 minutes before a kickoff until it, hourly
otherwise. Each sweep covers the time since the last successful sweep (at most
MAX_LOOKBACK). Handles: national insiders plus beat reporters of teams playing in
the next 36 hours, which keeps off-day sweeps small. Sweeps stop at
XAI_SWEEP_BUDGET_SHARE of the daily budget so on-move searches keep priority.

Run:  python -m pipelines.sweep_insiders [--force]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from common import insiders
from common.log import get_logger
from pipelines.search_x import budget_left, run_search, utcnow

log = get_logger("sweep_insiders")
MAX_LOOKBACK = timedelta(hours=6)
PREGAME = timedelta(minutes=90)


def interval_minutes(now: datetime, games: list[dict]) -> int:
    if any(g["kickoff_at"] - PREGAME <= now < g["kickoff_at"] for g in games):
        return 15
    return 60


def last_sweep_end(store) -> datetime | None:
    runs = [r for r in store.search_runs("sweep") if not r["error"]]
    return max((r["window_end"] for r in runs), default=None)


def due(store, now: datetime, games: list[dict]) -> bool:
    last = last_sweep_end(store)
    return last is None or now - last >= timedelta(minutes=interval_minutes(now, games)) - timedelta(seconds=30)


def sweep_handles(now: datetime, games: list[dict], include_unverified: bool = False) -> list[str]:
    soon = {t for g in games if now <= g["kickoff_at"] <= now + timedelta(hours=36) for t in (g["home"], g["away"])}
    return insiders.for_teams(soon, include_unverified)


def run(store, client, now: datetime | None = None, force: bool = False,
        include_unverified: bool = False) -> int | None:
    now = now or utcnow()
    games = store.games_between(now - timedelta(hours=6), now + timedelta(days=2))
    if not force and not due(store, now, games):
        return None
    if budget_left(store, "sweep", now) <= 0:
        return None  # cap reached; the spend alert already went out, so skip quietly
    last = last_sweep_end(store)
    start = max(last, now - MAX_LOOKBACK) if last else now - MAX_LOOKBACK
    posts = run_search(store, client, sweep_handles(now, games, include_unverified), start, now, "sweep", now=now)
    if posts is not None:
        log.info("sweep %s-%s: %d posts", f"{start:%H:%M}", f"{now:%H:%M}", len(posts))
    return None if posts is None else len(posts)


def main() -> None:
    from sources.xsearch import XSearchClient
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="sweep now even if not due")
    ap.add_argument("--include-unverified", action="store_true")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    run(store, XSearchClient(), force=args.force, include_unverified=args.include_unverified)


if __name__ == "__main__":
    main()
