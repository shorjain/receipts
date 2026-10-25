"""Poll Kalshi (every KALSHI_POLL_SECONDS) and The Odds API (credit-aware schedule),
store MARKETS + ODDS_SNAPSHOTS, and copy every pull to data/raw/.

Kalshi snapshots are stored when the mid moves at least min_prob_change_to_store
(0.5 pp), the derived line moves, or HEARTBEAT_MINUTES have passed. The heartbeat
keeps a reference point inside the 30-minute lookback so a jump after a long
quiet stretch is still detected.

Run:  python -m pipelines.ingest_odds --once       one pass (Odds API only if due)
      python -m pipelines.ingest_odds --loop       keep polling
      python -m pipelines.ingest_odds --once --force-odds-api
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common.log import get_logger
from common.raw import write_raw
from common.settings import env_int, thresholds
from sources import kalshi, odds_api

log = get_logger("ingest_odds")
ET = ZoneInfo("America/New_York")
HEARTBEAT_MINUTES = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def should_store(snap: dict, last: dict | None, min_change: float, now: datetime) -> bool:
    if last is None:
        return True
    if now - last["taken_at"] >= timedelta(minutes=HEARTBEAT_MINUTES):
        return True
    a, b = snap.get("implied_prob"), last.get("implied_prob")
    if a is not None and b is not None and abs(a - b) >= min_change - 1e-9:
        return True
    la, lb = snap.get("line"), last.get("line")
    if la is not None and lb is not None and abs(la - lb) >= 0.01:
        return True
    return (a is None) != (b is None)


def poll_kalshi(store, client: kalshi.KalshiClient, games: list[dict], now: datetime | None = None) -> int:
    now = now or utcnow()
    raw = {series: client.list_markets(series) for series in kalshi.SERIES}
    write_raw("kalshi", [{**m, "_series": s} for s, ms in raw.items() for m in ms], when=now.replace(tzinfo=timezone.utc))
    markets, snaps = kalshi.normalize(raw, games, now)
    min_change = thresholds()["moves"]["min_prob_change_to_store"]
    last = store.latest_snapshots([s["market_id"] for s in snaps])
    keep = [s for s in snaps if should_store(s, last.get(s["market_id"]), min_change, now)]
    store.upsert_markets(markets)
    store.insert_odds_snapshots(keep)
    log.info("kalshi: %d markets, stored %d/%d snapshots", len(markets), len(keep), len(snaps))
    return len(keep)


def odds_api_interval(now_utc: datetime, games: list[dict]) -> int | None:
    """Minutes between Odds API polls right now, or None to skip polling."""
    sched = thresholds()["odds_api_schedule"]
    upcoming = [g for g in games if now_utc < g["kickoff_at"] <= now_utc + timedelta(days=8)]
    if not upcoming:
        return None
    now_et = now_utc.replace(tzinfo=timezone.utc).astimezone(ET)
    if now_et.weekday() == 6 and now_et.hour >= sched["sunday_start_hour_et"]:
        todays = [g for g in upcoming
                  if g["kickoff_at"].replace(tzinfo=timezone.utc).astimezone(ET).date() == now_et.date()]
        if todays:
            return sched["sunday_minutes"]
    return sched["weekday_minutes"]


def odds_api_due(store, now: datetime, games: list[dict]) -> bool:
    interval = odds_api_interval(now, games)
    if interval is None:
        return False
    last = store.last_api_usage("odds_api")
    return last is None or now - last["called_at"] >= timedelta(minutes=interval) - timedelta(seconds=30)


def poll_odds_api(store, client: odds_api.OddsApiClient, games: list[dict], now: datetime | None = None) -> int:
    now = now or utcnow()
    live_ok = client.remaining is not None and client.remaining > env_int("ODDS_API_LIVE_MIN_CREDITS", 5000)
    events = client.odds()
    if not live_ok:  # drop started games; their in-game odds are not used for moves
        events = [e for e in events if odds_api._parse_ts(e["commence_time"]) > now]
    write_raw("odds_api", [{"event": e} for e in events], when=now.replace(tzinfo=timezone.utc))
    markets, snaps = odds_api.normalize(events, games, now)
    store.upsert_markets(markets)
    store.insert_odds_snapshots(snaps)
    log.info("odds_api: %d events, %d snapshots, %s credits left", len(events), len(snaps), client.remaining)
    return len(snaps)


def run_once(store, force_odds_api: bool = False) -> None:
    now = utcnow()
    games = store.games_between(now - timedelta(days=2), now + timedelta(days=10))
    if not games:
        log.warning("no games in GAMES for the next 10 days; run pipelines.load_nflverse first")
        return
    try:
        poll_kalshi(store, kalshi.KalshiClient(), games, now)
    except Exception as e:  # keep the other source running
        log.error("kalshi poll failed: %s", e)
    client = odds_api.OddsApiClient(store=store)
    if not client.key:
        log.info("ODDS_API_KEY not set; skipping The Odds API")
        return
    last = store.last_api_usage("odds_api")
    client.remaining = last["remaining"] if last else None
    if force_odds_api or odds_api_due(store, now, games):
        try:
            poll_odds_api(store, client, games, now)
        except odds_api.CreditsExhausted as e:
            log.warning("odds_api backing off: %s", e)


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--force-odds-api", action="store_true")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    if args.loop:
        period = env_int("KALSHI_POLL_SECONDS", 60)
        while True:
            started = time.time()
            run_once(store)
            time.sleep(max(1, period - (time.time() - started)))
    else:
        run_once(store, args.force_odds_api)


if __name__ == "__main__":
    main()
