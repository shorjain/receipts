"""The Odds API (v4): sportsbook moneylines, spreads, totals, and historical snapshots.

Cost model (per the docs): one call costs (number of markets) x (number of regions);
passing `bookmakers` bills every 10 books as one region. Historical calls cost 10x.
Remaining credits come back in the x-requests-remaining header and are logged.

Venues written for move detection:
  pinnacle   the sharp reference
  consensus  median no-vig probability / line across all returned books
Individual books are stored too but are not compared against each other, since
books copying each other would make every move look "multi-venue".
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone

import httpx

from common import teams
from common.settings import env, env_int

BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
MARKET_KEYS = {"h2h": "winner", "spreads": "spread", "totals": "total"}


class CreditsExhausted(RuntimeError):
    pass


class OddsApiClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0, store=None):
        self.key = api_key or env("ODDS_API_KEY")
        self.http = httpx.Client(timeout=timeout)
        self.store = store
        self.remaining: float | None = None
        self.bookmakers = [b.strip() for b in env("ODDS_API_BOOKMAKERS", "pinnacle").split(",") if b.strip()]

    def estimated_cost(self, markets: list[str], historical: bool = False) -> int:
        regions = max(1, math.ceil(len(self.bookmakers) / 10))
        return len(markets) * regions * (10 if historical else 1)

    def _get(self, path: str, params: dict, cost: int) -> tuple[dict | list, dict]:
        if not self.key:
            raise RuntimeError("ODDS_API_KEY is not set")
        floor = env_int("ODDS_API_MIN_CREDITS", 50)
        if self.remaining is not None and self.remaining - cost < floor:
            raise CreditsExhausted(f"{self.remaining} credits left, floor is {floor}")
        r = self.http.get(f"{BASE}{path}", params={**params, "apiKey": self.key})
        r.raise_for_status()
        rem = r.headers.get("x-requests-remaining")
        last = r.headers.get("x-requests-last")
        self.remaining = float(rem) if rem is not None else self.remaining
        if self.store is not None:
            self.store.log_api_usage("odds_api", path, float(last or cost), self.remaining)
        return r.json(), dict(r.headers)

    def odds(self, markets: tuple[str, ...] = ("h2h", "spreads", "totals")) -> list[dict]:
        data, _ = self._get(f"/sports/{SPORT}/odds", {
            "markets": ",".join(markets), "oddsFormat": "american",
            "bookmakers": ",".join(self.bookmakers), "dateFormat": "iso",
        }, self.estimated_cost(list(markets)))
        return data

    def historical_odds(self, when: datetime, markets: tuple[str, ...] = ("h2h", "spreads", "totals")) -> dict:
        """Returns {'timestamp', 'previous_timestamp', 'next_timestamp', 'data': [...]}."""
        data, _ = self._get(f"/historical/sports/{SPORT}/odds", {
            "markets": ",".join(markets), "oddsFormat": "american",
            "bookmakers": ",".join(self.bookmakers), "dateFormat": "iso",
            "date": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, self.estimated_cost(list(markets), historical=True))
        return data


# ---- math -------------------------------------------------------------------
def american_to_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def no_vig(p_a: float, p_b: float) -> tuple[float, float]:
    """Multiplicative vig removal for a two-way market."""
    s = p_a + p_b
    return p_a / s, p_b / s


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def match_game(event: dict, games: list[dict]) -> dict | None:
    try:
        home, away = teams.from_full_name(event["home_team"]), teams.from_full_name(event["away_team"])
    except KeyError:
        return None
    start = _parse_ts(event["commence_time"])
    for g in games:
        if g["home"] == home and g["away"] == away and abs(g["kickoff_at"] - start) <= timedelta(hours=12):
            return g
    return None


def _book_quote(mtype: str, market: dict, home_name: str) -> dict | None:
    outs = market.get("outcomes", [])
    if len(outs) != 2:
        return None
    if mtype == "total":
        over = next((o for o in outs if o["name"].lower() == "over"), None)
        under = next((o for o in outs if o["name"].lower() == "under"), None)
        first, second = over, under
    else:
        first = next((o for o in outs if o["name"] == home_name), None)
        second = next((o for o in outs if o["name"] != home_name), None)
    if not first or not second:
        return None
    p, _ = no_vig(american_to_prob(first["price"]), american_to_prob(second["price"]))
    return {"implied_prob": p, "price": float(first["price"]), "line": first.get("point")}


def normalize(events: list[dict], games: list[dict], taken_at: datetime,
              source: str = "odds_api") -> tuple[list[dict], list[dict]]:
    """Events from /odds (or historical 'data') -> (MARKETS rows, ODDS_SNAPSHOTS rows).

    Snapshots use taken_at (the poll time) so that every venue in one poll lines up.
    """
    markets, snaps = [], []
    for ev in events:
        g = match_game(ev, games)
        if not g:
            continue
        per_type: dict[str, list[dict]] = {}
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                mtype = MARKET_KEYS.get(mk["key"])
                if not mtype:
                    continue
                q = _book_quote(mtype, mk, ev["home_team"])
                if not q:
                    continue
                side = "over" if mtype == "total" else "home"
                mid = f"{bk['key']}:{g['game_id']}:{mtype}"
                markets.append({"market_id": mid, "venue": bk["key"], "game_id": g["game_id"],
                                "market_type": mtype, "line": None, "side": side, "source_ref": ev["id"]})
                snaps.append({"market_id": mid, "taken_at": taken_at, "bid": None, "ask": None,
                              "volume": None, "source": source, **q})
                per_type.setdefault(mtype, []).append(q)
        for mtype, qs in per_type.items():
            lines = [q["line"] for q in qs if q["line"] is not None]
            mid = f"consensus:{g['game_id']}:{mtype}"
            markets.append({"market_id": mid, "venue": "consensus", "game_id": g["game_id"],
                            "market_type": mtype, "line": None,
                            "side": "over" if mtype == "total" else "home", "source_ref": ev["id"]})
            snaps.append({"market_id": mid, "taken_at": taken_at, "bid": None, "ask": None,
                          "volume": float(len(qs)), "source": f"{source}_consensus",
                          "implied_prob": statistics.median(q["implied_prob"] for q in qs),
                          "price": None, "line": statistics.median(lines) if lines else None})
    return markets, snaps
