"""For each new line move, search tracked insiders from 60 minutes before the move
window through its end, store the posts, and link them in MOVE_POSTS.

Handles: national insiders plus the two teams' beat reporters, in batches. A game
that was searched within XAI_MOVE_COOLDOWN_MINUTES reuses those posts instead of
searching again. lead_minutes = move start - post time (negative = posted after).
relevance and attribution_score are filled by explain_moves (milestone 5).

explain_status: pending -> searched (or stays pending when the budget is spent).

Run:  python -m pipelines.search_moves --hours 24
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from common import insiders
from common.log import get_logger
from common.settings import env_float
from pipelines.search_x import run_search, utcnow

log = get_logger("search_moves")
PRE_WINDOW = timedelta(minutes=60)


def link_rows(move: dict, posts: list[dict]) -> list[dict]:
    rows = []
    for p in posts:
        if move["window_start"] - PRE_WINDOW <= p["posted_at"] <= move["window_end"]:
            lead = (move["window_start"] - p["posted_at"]).total_seconds() / 60
            rows.append({"move_id": move["move_id"], "post_id": p["post_id"],
                         "lead_minutes": round(lead, 1)})
    return rows


def recently_searched(store, game_id: str, window_end: datetime, now: datetime) -> bool:
    cooldown = timedelta(minutes=env_float("XAI_MOVE_COOLDOWN_MINUTES", 15))
    for r in store.search_runs("move", since=now - cooldown):
        if r["game_id"] == game_id and not r["error"] and r["window_end"] >= window_end:
            return True
    return False


def run(store, client, hours: float = 24, now: datetime | None = None,
        include_unverified: bool = False) -> int:
    now = now or utcnow()
    games = {g["game_id"]: g for g in store.games_between(now - timedelta(days=3), now + timedelta(days=10))}
    moves = [m for m in store.line_moves(now - timedelta(hours=hours), now, explain_status="pending")
             if m["window_end"] <= now]
    moves.sort(key=lambda m: -(m["magnitude_score"] or 0))
    done = 0
    for m in moves:
        g = games.get(m["game_id"])
        handles = insiders.for_teams({g["home"], g["away"]} if g else set(), include_unverified)
        start, end = m["window_start"] - PRE_WINDOW, m["window_end"]
        if recently_searched(store, m["game_id"], end, now):
            posts = store.posts_in_window(start, end, handles)
        else:
            focus = f"{g['away']} at {g['home']}" if g else None
            posts = run_search(store, client, handles, start, end, "move", m["move_id"], m["game_id"], focus, now)
            if posts is None:
                log.warning("budget spent; move %s stays pending", m["move_id"])
                break
            # Include posts already stored for this window (e.g. from sweeps).
            known = {p["post_id"] for p in posts}
            posts += [p for p in store.posts_in_window(start, end, handles) if p["post_id"] not in known]
        store.upsert_move_posts(link_rows(m, posts))
        store.upsert_line_moves([{"move_id": m["move_id"], "explain_status": "searched"}])
        done += 1
    if moves:
        log.info("searched %d/%d pending moves", done, len(moves))
    return done


def main() -> None:
    from sources.xsearch import XSearchClient
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--include-unverified", action="store_true")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    run(store, XSearchClient(), args.hours, include_unverified=args.include_unverified)


if __name__ == "__main__":
    main()
