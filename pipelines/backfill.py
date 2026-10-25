"""Backfill this season so the app has history on day one (milestone 7). Resumable.

Work is split into tasks recorded in BACKFILL_STATE; a finished task is never redone,
so stopping and restarting picks up where it left off.
  kalshi:<game>   1-minute Kalshi candles for both teams' winner markets, from the
                  market's open to kickoff, turned into the same derived P(home win)
                  series live polling stores (kalshi:<game>:winner)
  poly:<game>     Polymarket's minute price history for the game's moneyline
  detect:<game>   once both fetches are done: detect moves and compare venues for that
                  game's pre-kickoff window, then queue a search task per new move
  search:<move>   find the insider posts behind a past move. X search is used while
                  the backfill's own xAI cap (BACKFILL_XAI_USD) and the daily cap allow,
                  biggest and multi-venue moves first; after that, moves are linked to
                  posts already stored. explain_moves then explains them as usual.
  oddsapi:<day>   optional: one historical Odds API snapshot per game day, only when
                  BACKFILL_ODDS_API_CREDITS > 0 (30 credits each; off by default, since
                  the free tier can't buy enough snapshots to detect book moves and
                  nflverse already has closing lines)

Stored like live data: a point is kept when it moves >= 0.5 pp or 10 minutes pass.
Polymarket history has no bid/ask or volume, so it's treated as liquid (1-cent quote).

Runs inside `ingest_odds --loop` in short slices (the loop is the only DuckDB writer),
or on its own:  python -m pipelines.backfill [--seconds 0 for until done]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from common.log import get_logger
from common.raw import write_raw
from common.settings import env_bool, env_float, env_int, thresholds
from pipelines import compare_venues, detect_moves, search_moves
from pipelines.search_x import utcnow
from sources import kalshi, polymarket

log = get_logger("backfill")

CANDLE_CHUNK = timedelta(days=3)          # Kalshi returns at most ~5,000 candles per call
MAX_LOOKBACK = timedelta(days=14)         # earliest data fetched before a kickoff
REPLAN_EVERY = timedelta(hours=12)
HEARTBEAT = timedelta(minutes=10)
KIND_PRIORITY = {"kalshi": 4, "poly": 4, "detect": 3, "oddsapi": 2, "search": 1}


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def compress(points: list[tuple[datetime, float]], min_change: float, heartbeat: timedelta = HEARTBEAT):
    """Keep points that moved >= min_change from the last kept one, or after `heartbeat`."""
    kept: list[tuple[datetime, float]] = []
    for t, v in points:
        if not kept or abs(v - kept[-1][1]) >= min_change - 1e-9 or t - kept[-1][0] >= heartbeat:
            kept.append((t, v))
    return kept


def kalshi_series(home: list[dict], away: list[dict]) -> list[dict]:
    """Minute candles for both team markets -> derived rows (normalized P(home win)),
    with bid/ask from the home market and its rolling 24 h volume."""
    def mids(cs):
        out = {}
        for c in cs:
            b, a = c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars")
            if b is not None and a is not None:
                out[c["end_period_ts"]] = (float(b), float(a), float(c.get("volume_fp") or 0))
        return out
    h, a = mids(home), mids(away)
    rows, last_h, last_a, window, vol = [], None, None, deque(), 0.0
    for ts in sorted(set(h) | set(a)):
        if ts in h:
            last_h = h[ts]
            window.append((ts, last_h[2]))
            vol += last_h[2]
        if ts in a:
            last_a = a[ts]
        while window and window[0][0] <= ts - 86400:
            vol -= window.popleft()[1]
        if last_h is None:
            continue
        hb, ha, _ = last_h
        mh = (hb + ha) / 2
        p = mh
        if last_a is not None:
            ma = (last_a[0] + last_a[1]) / 2
            if mh + ma > 0:
                p = mh / (mh + ma)
        rows.append({"t": _ts(ts), "p": p, "bid": hb, "ask": ha, "volume": vol})
    return rows


def thin(rows: list[dict], min_change: float) -> list[dict]:
    kept_t = {t for t, _ in compress([(r["t"], r["p"]) for r in rows], min_change)}
    return [r for r in rows if r["t"] in kept_t]


# ---- planning ------------------------------------------------------------------------
def plan(store, kc, pc, now: datetime, season: int) -> int:
    """Queue tasks for finished games that don't have them yet."""
    known = {t["task_id"] for t in store.backfill_tasks()}
    games = [g for g in store.get_games(season)
             if g["game_type"] == "REG" and g["kickoff_at"] < now - timedelta(hours=4)]
    if not games:
        return 0
    by_game: dict[str, dict] = {}
    for m in kc.list_markets("KXNFLGAME", status="settled"):
        g = kalshi.match_game(m["event_ticker"], games)
        if g:
            side = kalshi.team_from_ticker(m["ticker"])
            d = by_game.setdefault(g["game_id"], {"tickers": {}, "open": m.get("open_time")})
            d["tickers"][side] = m["ticker"]
            d["open"] = min(d["open"] or m["open_time"], m["open_time"])
    first = min(g["kickoff_at"] for g in games) - MAX_LOOKBACK
    poly: dict[str, dict] = {}
    for m in pc.gamma_markets("moneyline", first, now, closed=True):
        e = polymarket.catalog_entry(m, games)
        if e:
            poly[e["game_id"]] = {"token": e["token_id"], "side": e["side"]}
    new = []
    for g in games:
        recency = g["kickoff_at"].timestamp() / 1e10      # newer games first within a kind
        for kind, detail in (("kalshi", by_game.get(g["game_id"])), ("poly", poly.get(g["game_id"]))):
            tid = f"{kind}:{g['game_id']}"
            if tid not in known:
                new.append({"task_id": tid, "kind": kind, "game_id": g["game_id"],
                            "priority": KIND_PRIORITY[kind] + recency,
                            "status": "pending" if detail else "skipped", "detail": detail or {},
                            "note": None if detail else "no market found"})
        tid = f"detect:{g['game_id']}"
        if tid not in known:
            new.append({"task_id": tid, "kind": "detect", "game_id": g["game_id"],
                        "priority": KIND_PRIORITY["detect"] + recency, "status": "pending", "detail": {}})
    credits = env_int("BACKFILL_ODDS_API_CREDITS", 0)
    if credits > 0:
        days = sorted({(g["kickoff_at"] - timedelta(hours=5)).date() for g in games})
        for d in days[-max(0, credits // 30):]:
            tid = f"oddsapi:{d}"
            if tid not in known:
                new.append({"task_id": tid, "kind": "oddsapi", "game_id": None, "status": "pending",
                            "priority": KIND_PRIORITY["oddsapi"], "detail": {"day": str(d)}})
    store.upsert_backfill_tasks(new + [{"task_id": "plan", "kind": "plan", "status": "done",
                                         "priority": 0, "detail": {"at": now.isoformat()}}])
    log.info("planned %d new backfill tasks for %d finished games", len(new), len(games))
    return len(new)


# ---- tasks ---------------------------------------------------------------------------
def _detail(task: dict) -> dict:
    d = task.get("detail")
    return json.loads(d) if isinstance(d, str) else (d or {})


def fetch_kalshi(store, kc, task: dict, game: dict) -> int:
    d = _detail(task)
    tickers = d.get("tickers", {})
    home_t = tickers.get(game["home"])
    if not home_t:
        return 0
    away_t = tickers.get(game["away"])
    open_at = datetime.fromisoformat(d["open"].replace("Z", "+00:00")).replace(tzinfo=None) if d.get("open") else None
    start = max(open_at or game["kickoff_at"] - MAX_LOOKBACK, game["kickoff_at"] - MAX_LOOKBACK)
    candles = {home_t: [], away_t: []} if away_t else {home_t: []}
    for ticker in candles:
        s = start
        while s < game["kickoff_at"]:
            e = min(s + CANDLE_CHUNK, game["kickoff_at"])
            candles[ticker] += kc.candlesticks("KXNFLGAME", ticker, s, e, 1)
            s = e
    rows = thin(kalshi_series(candles[home_t], candles.get(away_t, [])),
                thresholds()["moves"]["min_prob_change_to_store"])
    write_raw("backfill_kalshi", rows, name=game["game_id"])
    mid = f"kalshi:{game['game_id']}:winner"
    store.upsert_markets([{"market_id": mid, "venue": "kalshi", "game_id": game["game_id"], "market_type": "winner",
                           "line": None, "side": "home", "source_ref": "derived"}])
    store.insert_odds_snapshots([{"market_id": mid, "taken_at": r["t"], "implied_prob": r["p"], "price": None,
                                  "line": None, "bid": r["bid"], "ask": r["ask"], "volume": r["volume"],
                                  "source": "kalshi_derived"} for r in rows])
    return len(rows)


def fetch_poly(store, pc, task: dict, game: dict) -> int:
    d = _detail(task)
    hist = pc.prices_history(d["token"], game["kickoff_at"] - MAX_LOOKBACK, game["kickoff_at"], 1)
    flip = d["side"] != game["home"]
    pts = sorted({_ts(h["t"]).replace(second=0): (1 - h["p"] if flip else h["p"]) for h in hist}.items())
    kept = compress(pts, thresholds()["moves"]["min_prob_change_to_store"])
    write_raw("backfill_polymarket", [{"t": t, "p": p} for t, p in kept], name=game["game_id"])
    mid = f"polymarket:{game['game_id']}:winner"
    store.upsert_markets([{"market_id": mid, "venue": "polymarket", "game_id": game["game_id"],
                           "market_type": "winner", "line": None, "side": "home", "source_ref": "derived"}])
    store.insert_odds_snapshots([{"market_id": mid, "taken_at": t, "implied_prob": p, "price": None, "line": None,
                                  "bid": round(p - 0.005, 4), "ask": round(p + 0.005, 4), "volume": 1e6,
                                  "source": "polymarket_derived"} for t, p in kept])
    return len(kept)


def run_detect(store, game: dict, now: datetime) -> int:
    hours = MAX_LOOKBACK.total_seconds() / 3600
    detect_moves.run(store, hours=hours, now=game["kickoff_at"], game_id=game["game_id"])
    compare_venues.run(store, hours=hours, now=game["kickoff_at"], game_id=game["game_id"])
    moves = store.line_moves(game["kickoff_at"] - MAX_LOOKBACK, game["kickoff_at"],
                             game_id=game["game_id"], explain_status="pending")
    store.upsert_backfill_tasks([{
        "task_id": f"search:{m['move_id']}", "kind": "search", "game_id": game["game_id"], "status": "pending",
        "priority": KIND_PRIORITY["search"] + (m["magnitude_score"] or 0) / 100
        + (0.05 if m["venue_scope"] == "multi" else 0), "detail": {"move_id": m["move_id"]}} for m in moves])
    return len(moves)


def backfill_xai_left(store, season_start: datetime) -> float:
    return env_float("BACKFILL_XAI_USD", 3.0) - store.search_cost_since(season_start, ["backfill"])


def run_search_task(store, task: dict, game: dict, x_client, now: datetime) -> str:
    move = next((m for m in store.line_moves(game_id=game["game_id"]) if m["move_id"] == _detail(task)["move_id"]), None)
    if move is None or move["explain_status"] != "pending":
        return "done"
    season_start = datetime(game["season"], 8, 1)
    client = x_client if (x_client is not None and backfill_xai_left(store, season_start) > 0) else None
    ok = search_moves.process_move(store, client, move, game, now, purpose="backfill")
    if not ok:  # daily cap reached: link stored posts now rather than wait
        search_moves.process_move(store, None, move, game, now, purpose="backfill")
    return "done"


def fetch_odds_api_day(store, task: dict, now: datetime) -> int:
    from sources import odds_api
    day = datetime.fromisoformat(_detail(task)["day"])
    when = day.replace(hour=16)        # noon ET, before the early kickoffs
    client = odds_api.OddsApiClient(store=store)
    last = store.last_api_usage("odds_api")
    client.remaining = last["remaining"] if last else None
    data = client.historical_odds(when.replace(tzinfo=timezone.utc))
    taken = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
    games = store.games_between(when - timedelta(days=1), when + timedelta(days=2))
    markets, snaps = odds_api.normalize(data.get("data", []), games, taken, source="odds_api_hist")
    store.upsert_markets(markets)
    store.insert_odds_snapshots(snaps)
    return len(snaps)


# ---- driver --------------------------------------------------------------------------
_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        if name == "kalshi":
            _clients[name] = kalshi.KalshiClient()
        elif name == "poly":
            _clients[name] = polymarket.PolymarketClient()
        elif name == "x":
            from common.settings import env
            _clients[name] = None
            if env("XAI_API_KEY") and env_bool("XAI_ENABLED", True):
                from sources.xsearch import XSearchClient
                _clients[name] = XSearchClient()
    return _clients[name]


def _run_task(store, task: dict, game: dict | None, now: datetime) -> bool:
    """Run one task; True if it finished. Failures stay pending at lower priority."""
    try:
        if task["kind"] == "kalshi":
            note = f"{fetch_kalshi(store, _client('kalshi'), task, game)} points"
        elif task["kind"] == "poly":
            note = f"{fetch_poly(store, _client('poly'), task, game)} points"
        elif task["kind"] == "detect":
            fetches = [t for t in store.backfill_tasks() if t["game_id"] == task["game_id"]
                       and t["kind"] in ("kalshi", "poly")]
            if any(t["status"] == "pending" for t in fetches):
                return False
            note = f"{run_detect(store, game, now)} moves"
        elif task["kind"] == "search":
            note = run_search_task(store, task, game, _client("x"), now)
        elif task["kind"] == "oddsapi":
            note = f"{fetch_odds_api_day(store, task, now)} snapshots"
        else:
            return False
    except Exception as e:  # noqa: BLE001 - keep going; the task is retried next slice
        log.warning("backfill %s failed: %s", task["task_id"], e)
        store.upsert_backfill_tasks([{**task, "status": "pending", "note": f"retry: {e}"[:300],
                                      "priority": task["priority"] - 0.5}])
        return False
    store.upsert_backfill_tasks([{**task, "status": "done", "note": note}])
    return True


def step(store, seconds: float = 20, now: datetime | None = None, season: int | None = None) -> int:
    """Do backfill work for up to `seconds` (0 = until done). Returns tasks finished."""
    now = now or utcnow()
    if season is None:
        import nflreadpy
        season = nflreadpy.get_current_season()
    planned = next(iter(store.backfill_tasks(kind="plan")), None)
    at = _detail(planned).get("at") if planned else None
    if at is None or now - datetime.fromisoformat(at) >= REPLAN_EVERY:
        plan(store, _client("kalshi"), _client("poly"), now, season)
    started, done = time.time(), 0
    games = {g["game_id"]: g for g in store.get_games(season)}
    progressed = True
    while progressed:  # tasks can queue new ones (detect -> search); keep going while time allows
        progressed = False
        for task in store.backfill_tasks(status="pending"):
            if seconds and time.time() - started >= seconds:
                break
            if _run_task(store, task, games.get(task["game_id"]), now):
                done += 1
                progressed = True
        if seconds and time.time() - started >= seconds:
            break
    if done:
        left = len(store.backfill_tasks(status="pending"))
        log.info("backfill: %d tasks done this slice, %d left", done, left)
    return done


def progress(store) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in store.backfill_tasks():
        if t["kind"] != "plan":
            out.setdefault(t["kind"], {}).setdefault(t["status"], 0)
            out[t["kind"]][t["status"]] += 1
    return out


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=0, help="time budget; 0 = run until done")
    ap.add_argument("--status", action="store_true", help="print progress and exit")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    if not args.status:
        step(store, args.seconds)
    for kind, counts in progress(store).items():
        print(f"{kind:>8}: {counts}")


if __name__ == "__main__":
    main()
