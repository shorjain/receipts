"""Poll Kalshi and Polymarket (every KALSHI_POLL_SECONDS) and The Odds API
(credit-aware schedule), store MARKETS + ODDS_SNAPSHOTS, and copy every pull to data/raw/.

Polymarket's market catalog is rediscovered every 30 minutes; prices come from one
batched order-book call per poll.

Kalshi and Polymarket snapshots are stored when the mid moves at least min_prob_change_to_store
(0.5 pp), the derived line moves, or HEARTBEAT_MINUTES have passed. The heartbeat
keeps a reference point inside the 30-minute lookback so a jump after a long
quiet stretch is still detected.

Run:  python -m pipelines.ingest_odds --once       one pass (Odds API only if due)
      python -m pipelines.ingest_odds --loop       keep polling; every --analyze-every
                                                   minutes (default 10) also run
                                                   detect_moves and compare_venues, and
                                                   search X for new moves; run scheduled
                                                   insider sweeps when due; and every
                                                   --sync-every minutes (default 60) sync to
                                                   Snowflake. All in one process, since
                                                   DuckDB allows only one writer.
      python -m pipelines.ingest_odds --once --force-odds-api
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common.log import get_logger
from common.raw import write_raw
from common.settings import env, env_bool, env_float, env_int, thresholds
from sources import kalshi, odds_api, polymarket

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


class PolymarketPoller:
    """Keeps the Gamma catalog between polls; refreshes it every CATALOG_REFRESH_MINUTES."""

    def __init__(self, client: polymarket.PolymarketClient | None = None):
        self.client = client or polymarket.PolymarketClient()
        self.catalog: list[dict] = []
        self.refreshed_at: datetime | None = None

    def poll(self, store, games: list[dict], now: datetime | None = None) -> int:
        now = now or utcnow()
        stale = timedelta(minutes=polymarket.CATALOG_REFRESH_MINUTES)
        if self.refreshed_at is None or now - self.refreshed_at >= stale:
            self.catalog = polymarket.discover(self.client, games, now)
            self.refreshed_at = now
        books = self.client.books([e["token_id"] for e in self.catalog])
        write_raw("polymarket", [{"asset_id": b.get("asset_id"), "bids": b.get("bids", [])[-5:],
                                  "asks": b.get("asks", [])[-5:], "ts": b.get("timestamp")} for b in books],
                  when=now.replace(tzinfo=timezone.utc))
        markets, snaps = polymarket.normalize(self.catalog, books, games, now)
        min_change = thresholds()["moves"]["min_prob_change_to_store"]
        last = store.latest_snapshots([s["market_id"] for s in snaps])
        keep = [s for s in snaps if should_store(s, last.get(s["market_id"]), min_change, now)]
        store.upsert_markets(markets)
        store.insert_odds_snapshots(keep)
        log.info("polymarket: %d markets, stored %d/%d snapshots", len(markets), len(keep), len(snaps))
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


def daily_allowance(remaining: float | None, now_utc: datetime, cost: int, floor: int) -> int:
    """Calls allowed today so the month's credits last: (remaining - floor) spread over
    the days left in the month (credits reset monthly; calendar month approximates it)."""
    if remaining is None:
        return 1  # first call ever: learn the balance from the response headers
    now_et = now_utc.replace(tzinfo=timezone.utc).astimezone(ET)
    nxt = (now_et.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = (nxt.date() - now_et.date()).days
    return max(0, int((remaining - floor) / days_left // cost))


def odds_api_reason(store, now: datetime, games: list[dict], cost: int, remaining: float | None) -> str | None:
    """Why to poll The Odds API now (paced mode), or None."""
    sched = thresholds()["odds_api_schedule"]
    if not any(now < g["kickoff_at"] <= now + timedelta(days=8) for g in games):
        return None
    now_et = now.replace(tzinfo=timezone.utc).astimezone(ET)
    midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_et.astimezone(timezone.utc).replace(tzinfo=None)
    allowed = daily_allowance(remaining, now, cost, env_int("ODDS_API_MIN_CREDITS", 50))
    if store.api_calls_since("odds_api", midnight_utc) >= allowed:
        return None
    last = store.last_api_usage("odds_api")
    last_at = last["called_at"] if last else None
    for hhmm in sched.get("baseline_times_et", []):
        h, m = map(int, hhmm.split(":"))
        slot = midnight_et.replace(hour=h, minute=m).astimezone(timezone.utc).replace(tzinfo=None)
        if slot <= now and (last_at is None or last_at < slot):
            return f"baseline {hhmm} ET"
    if sched.get("on_move") and (last_at is None or now - last_at >= timedelta(minutes=sched["on_move_cooldown_minutes"])):
        since = last_at or now - timedelta(hours=6)
        fresh = [m for m in store.line_moves(since, now) if m["window_end"] > since
                 and any(v in (m["venues"] or "") for v in ("kalshi", "polymarket"))]
        if fresh:
            return f"move in {fresh[0]['game_id']}"
    return None


def odds_api_due(store, now: datetime, games: list[dict], cost: int = 3,
                 remaining: float | None = None) -> str | None:
    """Reason to poll now, or None. Mode comes from config/thresholds.yaml."""
    if thresholds()["odds_api_schedule"].get("mode", "paced") == "paced":
        return odds_api_reason(store, now, games, cost, remaining)
    interval = odds_api_interval(now, games)
    if interval is None:
        return None
    last = store.last_api_usage("odds_api")
    if last is None or now - last["called_at"] >= timedelta(minutes=interval) - timedelta(seconds=30):
        return f"every {interval} min"
    return None


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


def run_once(store, force_odds_api: bool = False, pm_poller: PolymarketPoller | None = None,
             use_odds_api: bool = True) -> None:
    now = utcnow()
    games = store.games_between(now - timedelta(days=2), now + timedelta(days=10))
    if not games:
        log.warning("no games in GAMES for the next 10 days; run pipelines.load_nflverse first")
        return
    try:
        poll_kalshi(store, kalshi.KalshiClient(), games, now)
    except Exception as e:  # keep the other sources running
        log.error("kalshi poll failed: %s", e)
    try:
        (pm_poller or PolymarketPoller()).poll(store, games, now)
    except Exception as e:
        log.error("polymarket poll failed: %s", e)
    if not use_odds_api:
        return
    client = odds_api.OddsApiClient(store=store)
    if not client.key:
        log.info("ODDS_API_KEY not set; skipping The Odds API")
        return
    last = store.last_api_usage("odds_api")
    client.remaining = last["remaining"] if last else None
    reason = "forced" if force_odds_api else odds_api_due(
        store, now, games, client.estimated_cost(["h2h", "spreads", "totals"]), client.remaining)
    if reason:
        log.info("odds_api poll: %s", reason)
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
    ap.add_argument("--analyze-every", type=float, default=10,
                    help="minutes between detect_moves + compare_venues runs in --loop (0 = never)")
    ap.add_argument("--snapshot-every", type=float, default=5,
                    help="minutes between read-only snapshots for the UI/API (0 = never)")
    ap.add_argument("--no-odds-api", action="store_true",
                    help="don't poll The Odds API (e.g. to save free-tier credits)")
    ap.add_argument("--no-x", action="store_true", help="skip all X searches in --loop")
    ap.add_argument("--no-sweeps", action="store_true", help="keep on-move searches, skip scheduled sweeps")
    ap.add_argument("--sync-every", type=float, default=env_float("SNOWFLAKE_SYNC_MINUTES", 60),
                    help="minutes between Snowflake syncs in --loop (0 = never; needs SNOWFLAKE_ACCOUNT)")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    if args.loop:
        period = env_int("KALSHI_POLL_SECONDS", 60)
        from pipelines import compare_venues, detect_moves, explain_moves, search_moves, sweep_insiders, sync_snowflake
        pm_poller = PolymarketPoller()
        last_analysis = last_sync = last_snapshot = 0.0
        can_sync = store.dialect == "duckdb" and bool(env("SNOWFLAKE_ACCOUNT")) and args.sync_every > 0
        x_client = None
        if env("XAI_API_KEY") and env_bool("XAI_ENABLED", True) and not args.no_x:
            from sources.xsearch import XSearchClient
            x_client = XSearchClient()
        while True:
            started = time.time()
            run_once(store, pm_poller=pm_poller, use_odds_api=not args.no_odds_api)
            if args.analyze_every and started - last_analysis >= args.analyze_every * 60:
                try:
                    detect_moves.run(store, hours=24)
                    compare_venues.run(store, hours=24)
                    search_moves.run(store, x_client, hours=24)  # x_client None: stored posts only
                    if store.line_moves(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24),
                                        explain_status="searched"):
                        ai = explain_moves.get_ai()  # Snowflake Cortex; woken only when needed
                        if ai:
                            try:
                                explain_moves.run(store, ai, hours=24)
                            finally:
                                ai.suspend_warehouse()
                                ai.close()
                except Exception as e:
                    log.error("analysis failed: %s", e)
                last_analysis = started
            if x_client and not args.no_sweeps:
                try:
                    sweep_insiders.run(store, x_client)  # no-op unless due
                except Exception as e:
                    log.error("sweep failed: %s", e)
            if store.dialect == "duckdb" and args.snapshot_every and started - last_snapshot >= args.snapshot_every * 60:
                try:
                    from store import snapshot_path
                    store.snapshot(snapshot_path())
                except Exception as e:
                    log.error("UI snapshot failed: %s", e)
                last_snapshot = started
            if can_sync and started - last_sync >= args.sync_every * 60:
                try:
                    sync_snowflake.run(store)
                except Exception as e:  # Snowflake down must not stop local polling
                    log.error("snowflake sync failed: %s", e)
                last_sync = started
            time.sleep(max(1, period - (time.time() - started)))
    else:
        run_once(store, args.force_odds_api, use_odds_api=not args.no_odds_api)


if __name__ == "__main__":
    main()
