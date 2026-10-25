"""Kalshi market data (public REST) and normalization to MARKETS / ODDS_SNAPSHOTS.

Series used:
  KXNFLGAME   one binary market per team: "<Team> wins"
  KXNFLSPREAD ladder per team: "<Team> wins by over X.5 points?"
  KXNFLTOTAL  ladder: "over X.5 points scored?"

The ladders are turned into one implied line per game by finding where the
probability curve crosses 50% (see implied_median). Spreads use home betting
convention: home -3.5 means the home team is favored by 3.5.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from common import teams
from common.settings import env, env_float

SERIES = {"KXNFLGAME": "winner", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total"}
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ---- HTTP --------------------------------------------------------------------
class KalshiClient:
    def __init__(self, base_url: str | None = None, timeout: float = 20.0):
        self.base = base_url or env("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
        self.http = httpx.Client(timeout=timeout)

    def get(self, path: str, params: dict | None = None, retries: int = 5) -> dict:
        """GET with exponential backoff on 429 / 5xx (public tier is rate limited)."""
        for attempt in range(retries + 1):
            r = self.http.get(f"{self.base}{path}", params=params)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries:
                    r.raise_for_status()
                time.sleep(float(r.headers.get("Retry-After") or 0) or min(30.0, 0.5 * 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    def list_markets(self, series_ticker: str, status: str | None = "open") -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "limit": 1000}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = self.get("/markets", params)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                return out

    def candlesticks(self, series_ticker: str, ticker: str, start: datetime, end: datetime,
                     period_minutes: int = 1) -> list[dict]:
        """period_minutes must be 1, 60 or 1440 per Kalshi docs."""
        data = self.get(f"/series/{series_ticker}/markets/{ticker}/candlesticks", {
            "start_ts": int(start.replace(tzinfo=start.tzinfo or timezone.utc).timestamp()),
            "end_ts": int(end.replace(tzinfo=end.tzinfo or timezone.utc).timestamp()),
            "period_interval": period_minutes,
        })
        return data.get("candlesticks", [])


# ---- parsing helpers -------------------------------------------------------
def _f(v) -> float | None:
    return None if v in (None, "") else float(v)


def parse_event_ticker(event_ticker: str) -> tuple[date, tuple[str, str]] | None:
    """'KXNFLGAME-26NOV02CHISEA' -> (2026-11-02, ('CHI', 'SEA'))."""
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", event_ticker)
    if not m:
        return None
    yy, mon, dd, pair = m.groups()
    split = teams.split_pair(pair)
    if not split or mon not in _MONTHS:
        return None
    return date(2000 + int(yy), _MONTHS[mon], int(dd)), split


def match_game(event_ticker: str, games: list[dict]) -> dict | None:
    """Find the GAMES row for a Kalshi event (same two teams, kickoff within a day)."""
    parsed = parse_event_ticker(event_ticker)
    if not parsed:
        return None
    day, pair = parsed
    for g in games:
        if {g["home"], g["away"]} == set(pair):
            ko = g["kickoff_at"]
            ko_et = (ko.replace(tzinfo=timezone.utc) - timedelta(hours=5)).date()
            if abs((ko_et - day).days) <= 1:
                return g
    return None


def quote(m: dict) -> dict:
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None and ask > 0 else _f(m.get("last_price_dollars"))
    return {
        "bid": bid, "ask": ask, "mid": mid, "price": _f(m.get("last_price_dollars")),
        "volume": _f(m.get("volume_24h_fp")), "strike": _f(m.get("floor_strike")),
    }


def is_liquid(q: dict, max_spread: float | None = None, min_volume: float | None = None) -> bool:
    max_spread = env_float("KALSHI_MAX_SPREAD", 0.04) if max_spread is None else max_spread
    min_volume = env_float("KALSHI_MIN_VOLUME", 500) if min_volume is None else min_volume
    if q["bid"] is None or q["ask"] is None or q["bid"] <= 0:
        return False
    return round(q["ask"] - q["bid"], 4) < max_spread and (q["volume"] or 0) >= min_volume


def implied_median(points: list[tuple[float, float]]) -> float | None:
    """Given (x, P(X > x)) points, return x where the curve crosses 0.5.

    Probabilities are forced non-increasing in x first (noisy quotes can invert).
    Returns None when the ladder doesn't bracket 0.5.
    """
    if len(points) < 2:
        return None
    pts = sorted(points)
    mono, running = [], 1.0
    for x, p in pts:
        running = min(running, p)
        mono.append((x, running))
    for (x0, p0), (x1, p1) in zip(mono, mono[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return x0 + (p0 - 0.5) * (x1 - x0) / (p0 - p1)
    return None


def team_from_ticker(ticker: str) -> str | None:
    m = re.match(r"^([A-Z]+)\d*$", ticker.rsplit("-", 1)[-1])
    try:
        return teams.canon(m.group(1)) if m else None
    except KeyError:
        return None


# ---- normalization ---------------------------------------------------------
def normalize(series_markets: dict[str, list[dict]], games: list[dict], taken_at: datetime,
              max_spread: float | None = None, min_volume: float | None = None
              ) -> tuple[list[dict], list[dict]]:
    """Turn raw markets from each series into (MARKETS rows, ODDS_SNAPSHOTS rows).

    Emits one raw snapshot per Kalshi ticker plus one derived snapshot per game and
    market type (market_id 'kalshi:<game_id>:<type>'), which is what move detection reads.
    """
    markets, snaps = [], []
    by_game: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    game_index = {g["game_id"]: g for g in games}
    for series, raw in series_markets.items():
        mtype = SERIES[series]
        for m in raw:
            g = match_game(m["event_ticker"], games)
            if not g:
                continue
            q = quote(m)
            side = team_from_ticker(m["ticker"]) if mtype != "total" else "over"
            mid = f"kalshi:{m['ticker']}"
            markets.append({"market_id": mid, "venue": "kalshi", "game_id": g["game_id"],
                            "market_type": mtype, "line": q["strike"], "side": side,
                            "source_ref": m["ticker"]})
            snaps.append({"market_id": mid, "taken_at": taken_at, "implied_prob": q["mid"],
                          "price": q["price"], "line": q["strike"], "bid": q["bid"],
                          "ask": q["ask"], "volume": q["volume"], "source": "kalshi_rest"})
            by_game.setdefault((g["game_id"], mtype), []).append((q, {"side": side}))

    for (game_id, mtype), items in by_game.items():
        g = game_index[game_id]
        derived = _derive(g, mtype, items, max_spread, min_volume)
        if derived is None:
            continue
        mid = f"kalshi:{game_id}:{mtype}"
        markets.append({"market_id": mid, "venue": "kalshi", "game_id": game_id,
                        "market_type": mtype, "line": None,
                        "side": "over" if mtype == "total" else "home", "source_ref": "derived"})
        snaps.append({"market_id": mid, "taken_at": taken_at, "source": "kalshi_derived", **derived})
    return markets, snaps


def _derive(g: dict, mtype: str, items: list[tuple[dict, dict]], max_spread, min_volume) -> dict | None:
    liquid = [(q, meta) for q, meta in items if is_liquid(q, max_spread, min_volume)]
    if mtype == "winner":
        home = next((q for q, m in items if m["side"] == g["home"]), None)
        away = next((q for q, m in items if m["side"] == g["away"]), None)
        if not home or home["mid"] is None:
            return None
        p = home["mid"]
        if away and away["mid"] is not None and home["mid"] + away["mid"] > 0:
            p = home["mid"] / (home["mid"] + away["mid"])
        return {"implied_prob": p, "price": home["price"], "line": None, "bid": home["bid"],
                "ask": home["ask"], "volume": home["volume"]}
    pts = []
    for q, meta in liquid:
        if q["strike"] is None or q["mid"] is None:
            continue
        if mtype == "total":
            pts.append((q["strike"], q["mid"]))
        elif meta["side"] == g["home"]:
            pts.append((q["strike"], q["mid"]))          # P(home margin > s)
        elif meta["side"] == g["away"]:
            pts.append((-q["strike"], 1 - q["mid"]))     # P(home margin > -s)
    median = implied_median(pts)
    if median is None:
        return None
    # Quote of the rung nearest 50% stands in for the market's bid/ask.
    near = min(liquid, key=lambda qm: abs((qm[0]["mid"] or 0) - 0.5))[0]
    line = round(median, 2) if mtype == "total" else round(-median, 2)
    return {"implied_prob": None, "price": None, "line": line, "bid": near["bid"],
            "ask": near["ask"], "volume": sum(q["volume"] or 0 for q, _ in liquid)}
