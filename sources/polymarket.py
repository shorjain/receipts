"""Polymarket NFL game markets (read-only, public APIs).

Two steps, because a full event listing is ~50 MB:
  discover()  Gamma API /markets filtered to sports_market_types moneyline, spreads
              and totals for the NFL tag. Builds a catalog of one token per market
              (outcome 0) mapped to our game, market type, side and strike. Refresh
              every CATALOG_REFRESH_MINUTES.
  books()     CLOB POST /books for up to 500 tokens per call: live best bid / ask.

Token semantics (outcome 0):
  moneyline  outcomes [TeamA, TeamB]   -> P(TeamA wins)
  spreads    outcomes [Team, Opp], line -> P(Team's margin > -line)   (line is -1.5, -2.5...)
  totals     outcomes [Over, Under]     -> P(total > line)

Polymarket blocks trading from the US on its international exchange; reading prices
is unaffected. This module never trades.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from common import teams
from common.games import match_by_teams
from common.settings import env_float
from sources import ladder

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
NFL_TAG_ID = 450
MARKET_TYPES = {"moneyline": "winner", "spreads": "spread", "totals": "total"}
BOOKS_BATCH = 500
CATALOG_REFRESH_MINUTES = 30

_BY_NICK = {nick.lower(): abbr for abbr, (_, nick) in teams.TEAMS.items()}


class PolymarketClient:
    def __init__(self, timeout: float = 30.0):
        self.http = httpx.Client(timeout=timeout)

    def _req(self, method: str, url: str, retries: int = 4, **kw):
        for attempt in range(retries + 1):
            r = self.http.request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries:
                    r.raise_for_status()
                time.sleep(min(20.0, 0.5 * 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()

    def gamma_markets(self, sports_market_type: str, end_min: datetime, end_max: datetime) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self._req("GET", f"{GAMMA}/markets", params={
                "closed": "false", "tag_id": NFL_TAG_ID, "sports_market_types": sports_market_type,
                "end_date_min": end_min.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end_max.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 100, "offset": offset,
            })
            out.extend(page)
            if len(page) < 100:
                return out
            offset += 100

    def books(self, token_ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(token_ids), BOOKS_BATCH):
            chunk = token_ids[i:i + BOOKS_BATCH]
            out.extend(self._req("POST", f"{CLOB}/books", json=[{"token_id": t} for t in chunk]))
        return out

    def prices_history(self, token_id: str, start: datetime, end: datetime, fidelity_minutes: int = 1) -> list[dict]:
        """[{'t': unix seconds, 'p': price}] for one token (used by backfill)."""
        data = self._req("GET", f"{CLOB}/prices-history", params={
            "market": token_id, "fidelity": fidelity_minutes,
            "startTs": int(start.replace(tzinfo=start.tzinfo or timezone.utc).timestamp()),
            "endTs": int(end.replace(tzinfo=end.tzinfo or timezone.utc).timestamp()),
        })
        return data.get("history", [])


# ---- catalog -------------------------------------------------------------------
def _team(name: str) -> str | None:
    try:
        return _BY_NICK.get(name.lower()) or teams.canon(name)
    except KeyError:
        return None


def parse_event_ticker(ticker: str) -> tuple[date, tuple[str, str]] | None:
    """'nfl-cin-bal-2026-10-25' -> (2026-10-25, ('CIN', 'BAL'))."""
    parts = ticker.split("-")
    if len(parts) != 6 or parts[0] != "nfl":
        return None
    try:
        pair = (teams.canon(parts[1]), teams.canon(parts[2]))
        return date(int(parts[3]), int(parts[4]), int(parts[5])), pair
    except (KeyError, ValueError):
        return None


def catalog_entry(m: dict, games: list[dict]) -> dict | None:
    mtype = MARKET_TYPES.get(m.get("sportsMarketType"))
    events = m.get("events") or []
    if not mtype or not events:
        return None
    parsed = parse_event_ticker(events[0].get("ticker", ""))
    if not parsed:
        return None
    g = match_by_teams(parsed[1], parsed[0], games)
    if not g:
        return None
    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    if mtype == "total":
        if outcomes[0].lower() != "over" or m.get("line") is None:
            return None
        side, strike = "over", float(m["line"])
    else:
        side = _team(outcomes[0])
        if side not in (g["home"], g["away"]):
            return None
        strike = -float(m["line"]) if mtype == "spread" and m.get("line") is not None else None
        if mtype == "spread" and strike is None:
            return None
    return {"token_id": tokens[0], "gamma_id": m["id"], "game_id": g["game_id"],
            "market_type": mtype, "side": side, "strike": strike,
            "volume": float(m.get("volume24hr") or 0), "question": m["question"]}


def discover(client: PolymarketClient, games: list[dict], now: datetime) -> list[dict]:
    """Catalog of NFL game markets for the given games (moneyline, spreads, totals)."""
    end_min, end_max = now - timedelta(days=1), now + timedelta(days=10)
    out = []
    for smt in MARKET_TYPES:
        for m in client.gamma_markets(smt, end_min, end_max):
            e = catalog_entry(m, games)
            if e:
                out.append(e)
    return out


# ---- normalization ---------------------------------------------------------------
def book_quote(book: dict, volume: float) -> dict:
    bids = [float(b["price"]) for b in book.get("bids", [])]
    asks = [float(a["price"]) for a in book.get("asks", [])]
    bid, ask = (max(bids) if bids else None), (min(asks) if asks else None)
    last = book.get("last_trade_price")
    last = float(last) if last not in (None, "") else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else last
    return {"bid": bid, "ask": ask, "mid": mid, "price": last, "volume": volume}


def _complement(q: dict) -> dict:
    flip = lambda v: None if v is None else round(1 - v, 6)
    return {"bid": flip(q["ask"]), "ask": flip(q["bid"]), "mid": flip(q["mid"]),
            "price": flip(q["price"]), "volume": q["volume"], "strike": q.get("strike")}


def normalize(catalog: list[dict], books: list[dict], games: list[dict], taken_at: datetime,
              max_spread: float | None = None, min_volume: float | None = None
              ) -> tuple[list[dict], list[dict]]:
    """Catalog + CLOB books -> (MARKETS rows, ODDS_SNAPSHOTS rows), raw plus derived
    per game ('polymarket:<game_id>:<type>'), mirroring sources/kalshi.normalize."""
    max_spread = env_float("POLYMARKET_MAX_SPREAD", 0.04) if max_spread is None else max_spread
    min_volume = env_float("POLYMARKET_MIN_VOLUME", 500) if min_volume is None else min_volume
    by_token = {b.get("asset_id"): b for b in books}
    game_index = {g["game_id"]: g for g in games}
    markets, snaps = [], []
    by_game: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for e in catalog:
        book = by_token.get(e["token_id"])
        if not book:
            continue
        q = {**book_quote(book, e["volume"]), "strike": e["strike"]}
        mid = f"polymarket:{e['gamma_id']}"
        markets.append({"market_id": mid, "venue": "polymarket", "game_id": e["game_id"],
                        "market_type": e["market_type"], "line": e["strike"], "side": e["side"],
                        "source_ref": e["token_id"]})
        snaps.append({"market_id": mid, "taken_at": taken_at, "implied_prob": q["mid"],
                      "price": q["price"], "line": e["strike"], "bid": q["bid"], "ask": q["ask"],
                      "volume": q["volume"], "source": "polymarket_clob"})
        items = by_game.setdefault((e["game_id"], e["market_type"]), [])
        items.append((q, e["side"]))
        if e["market_type"] == "winner":  # one market per game; add the other team's side
            g = game_index[e["game_id"]]
            other = g["away"] if e["side"] == g["home"] else g["home"]
            items.append((_complement(q), other))
    for (game_id, mtype), items in by_game.items():
        derived = ladder.derive(game_index[game_id], mtype, items, max_spread, min_volume)
        if derived is None:
            continue
        mid = f"polymarket:{game_id}:{mtype}"
        markets.append({"market_id": mid, "venue": "polymarket", "game_id": game_id,
                        "market_type": mtype, "line": None,
                        "side": "over" if mtype == "total" else "home", "source_ref": "derived"})
        snaps.append({"market_id": mid, "taken_at": taken_at, "source": "polymarket_derived", **derived})
    return markets, snaps
