"""Shared x_search runner: budget check, handle batching, verification, storage.

Every call is logged to SEARCH_RUNS with its cost estimate, citations and the
rejected citations (and why), whether or not it found anything.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx

from common.log import get_logger
from common.notify import notify
from common.raw import write_raw
from common.settings import env_float
from sources import xsearch

log = get_logger("search_x")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# One-off setup searches (handle verification) don't count against the daily budget.
BUDGETED = ("move", "sweep", "backfill")


def spent_today(store, now: datetime | None = None) -> float:
    now = now or utcnow()
    return store.search_cost_since(now.replace(hour=0, minute=0, second=0, microsecond=0), BUDGETED)


def budget_left(store, purpose: str, now: datetime | None = None) -> float:
    """Dollars this purpose may still spend today. Sweeps stop at SWEEP share of the
    daily budget so on-move searches always have room."""
    if purpose not in BUDGETED:
        return float("inf")
    budget = env_float("XAI_DAILY_BUDGET_USD", 2.0)
    cap = budget * (env_float("XAI_SWEEP_BUDGET_SHARE", 0.8) if purpose == "sweep" else 1.0)
    return cap - spent_today(store, now)


_notified: set[tuple[str, str]] = set()


def check_spend_alerts(store, now: datetime | None = None, send=notify) -> list[str]:
    """Notify once per day when estimated spend passes XAI_NOTIFY_USD, and when it
    reaches the daily cap (searches pause until midnight UTC)."""
    now = now or utcnow()
    day, spent = f"{now:%Y-%m-%d}", spent_today(store, now)
    budget, warn = env_float("XAI_DAILY_BUDGET_USD", 2.0), env_float("XAI_NOTIFY_USD", 2.0)
    sent = []
    if spent >= budget and (day, "cap") not in _notified:
        _notified.add((day, "cap"))
        sent.append("cap")
        send("NFL Lines: X search paused", f"Today's xAI spend is ~${spent:.2f}, the ${budget:.2f} daily cap. "
                                           "Searches resume at midnight UTC.")
    elif spent >= warn and (day, "warn") not in _notified and warn < budget:
        _notified.add((day, "warn"))
        sent.append("warn")
        send("NFL Lines: xAI spend", f"Today's X searches have cost ~${spent:.2f} (cap ${budget:.2f}/day).")
    return sent


def run_search(store, client, handles: list[str], start: datetime, end: datetime, purpose: str,
               move_id: str | None = None, game_id: str | None = None, focus: str | None = None,
               now: datetime | None = None) -> list[dict] | None:
    """Search `handles` in batches; store verified posts. Returns posts, or None when
    the budget stopped the search before it started."""
    now = now or utcnow()
    http = httpx.Client(timeout=15, follow_redirects=True)
    all_posts: list[dict] = []
    for batch in xsearch.batch_handles(handles):
        if budget_left(store, purpose, now) <= 0:
            log.warning("xAI daily budget reached; skipping %s search for %s", purpose, batch)
            return None if not all_posts else all_posts
        res = client.search(batch, start, end, focus)
        ids = [pid for u in res.citations if (pid := xsearch.post_id_from_url(u))]
        posts, rejected = xsearch.verify_posts(res.citations, batch,
                                               oembed=lambda pid: xsearch.fetch_oembed(pid, http),
                                               known=store.posts_by_id(ids))
        store.upsert_posts(posts)
        write_raw("posts", posts)
        store.insert_search_run({
            "run_id": uuid.uuid4().hex[:16], "purpose": purpose, "window_start": start,
            "window_end": end, "handles": ",".join(batch),
            "raw_response": json.dumps({"content": res.content, "usage": res.usage,
                                        "tool_calls": res.tool_calls, "rejected": rejected}),
            "citations": json.dumps(res.citations), "error": res.error,
            "cost_estimate_usd": res.cost_estimate_usd, "ran_at": now,
            "move_id": move_id, "game_id": game_id,
            "posts_kept": len(posts), "posts_rejected": len(rejected),
        })
        log.info("%s search %s: %d posts kept, %d rejected, ~$%.3f%s", purpose, batch[:3] + (["..."] if len(batch) > 3 else []),
                 len(posts), len(rejected), res.cost_estimate_usd, f" ERROR {res.error}" if res.error else "")
        all_posts += posts
        check_spend_alerts(store, now)
    return all_posts
