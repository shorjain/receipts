"""Score each insider per season and over the last 4 weeks (Feature B, step 3).

  accuracy        Bayesian-smoothed, weighted: (sum w*correct + 2) / (sum w + 4) over
                  resolved claims, where w = 0.5 for hedged claims (expected / rumor)
                  and w = 2 for a confirmed claim that was wrong. Shown next to the raw
                  hit rate and n so three lucky claims don't top the board.
  original_ratio  claims that don't credit anyone else / all claims.
  scoop_rate      of the insider's own (uncredited) claims, the share where they were
                  first among tracked accounts in that story.
  median_lead_minutes  for their scoops that others later posted: minutes until the
                  next tracked account posted the same story.
  market_impact   mean excess odds move after their own claims about a game: |P(home
                  win) 30 min after - at the post| minus the game's typical 30-minute
                  move over the prior 24 h (Kalshi, else Polymarket, else consensus).
  credibility_weight  0-1, see credibility_weight() below; used by explain_moves.

Run:  python -m pipelines.score_insiders [--season 2026]
"""
from __future__ import annotations

import argparse
import statistics
from datetime import datetime, timedelta

from common.log import get_logger
from pipelines import detect_moves
from pipelines.search_x import utcnow

log = get_logger("score_insiders")

WINDOWS = {"season": None, "last4w": timedelta(days=28)}
VENUE_ORDER = ("kalshi", "polymarket", "consensus", "pinnacle")
IMPACT_SCALE = 0.02   # an average excess move of 2 pp counts as full market impact
SHRINK_N = 5          # resolved claims at which the weight is half data, half prior


def credibility_weight(accuracy: float | None, scoop_rate: float | None, market_impact: float | None,
                       original_ratio: float | None, n_resolved: int) -> float:
    """The one place to tune how much each signal counts.

    blend  = 0.60 accuracy + 0.15 scoop_rate + 0.15 impact (excess move / 2 pp, capped
             at 1) + 0.10 original_ratio; a missing signal counts as a neutral 0.5.
    weight = blend shrunk toward 0.5 by n_resolved / (n_resolved + 5), so a new
             insider starts at 0.5 and earns their way up or down.
    """
    def n(v):
        return 0.5 if v is None else v
    impact = None if market_impact is None else min(1.0, max(0.0, market_impact / IMPACT_SCALE))
    blend = 0.60 * n(accuracy) + 0.15 * n(scoop_rate) + 0.15 * n(impact) + 0.10 * n(original_ratio)
    shrink = n_resolved / (n_resolved + SHRINK_N)
    return round(shrink * blend + (1 - shrink) * 0.5, 4)


def _missing(v) -> bool:
    """None, NaN (pandas fills gaps with NaN, which is truthy) or empty text."""
    return v is None or v == "" or (isinstance(v, float) and v != v)


def claim_weight(c: dict) -> float:
    hedged = c.get("hedging") in ("expected", "rumor")
    if c.get("outcome") == "incorrect" and not hedged:
        return 2.0
    return 0.5 if hedged else 1.0


def author_stats(claims: list[dict]) -> dict:
    resolved = [c for c in claims if c.get("outcome") in ("correct", "incorrect")]
    wsum = sum(claim_weight(c) for c in resolved)
    wcorrect = sum(claim_weight(c) for c in resolved if c["outcome"] == "correct")
    own = [c for c in claims if _missing(c.get("credited_handle"))]
    scoops = [c for c in own if c.get("is_original") is True]
    impacts = [c["market_move"] for c in own if not _missing(c.get("market_move"))]
    leads = [c["_lead"] for c in scoops if not _missing(c.get("_lead"))]
    return {
        "n_claims": len(claims), "n_resolved": len(resolved),
        "accuracy": round((wcorrect + 2) / (wsum + 4), 4),
        "raw_hit_rate": round(sum(c["outcome"] == "correct" for c in resolved) / len(resolved), 4) if resolved else None,
        "original_ratio": round(len(own) / len(claims), 4) if claims else None,
        "scoop_rate": round(len(scoops) / len(own), 4) if own else None,
        "median_lead_minutes": round(statistics.median(leads), 1) if leads else None,
        "market_impact": round(statistics.mean(impacts), 4) if impacts else None,
    }


def add_story_leads(claims: list[dict]) -> None:
    """For each scoop, minutes until the next tracked account posted the same story."""
    by_story: dict[str, list[dict]] = {}
    for c in claims:
        if not _missing(c.get("story_id")):
            by_story.setdefault(c["story_id"], []).append(c)
    for cs in by_story.values():
        cs.sort(key=lambda c: c["posted_at"])
        for c in cs:
            if c.get("is_original") is True:
                later = [o for o in cs if o["author"] != c["author"] and o["posted_at"] > c["posted_at"]]
                c["_lead"] = (later[0]["posted_at"] - c["posted_at"]).total_seconds() / 60 if later else None


def compute_scores(claims: list[dict], now: datetime, season: int) -> list[dict]:
    add_story_leads(claims)
    rows = []
    for window, span in WINDOWS.items():
        pool = [c for c in claims if span is None or c["posted_at"] >= now - span]
        by_author: dict[str, list[dict]] = {}
        for c in pool:
            by_author.setdefault(c["author"], []).append(c)
        for author, cs in by_author.items():
            s = author_stats(cs)
            s["credibility_weight"] = credibility_weight(s["accuracy"], s["scoop_rate"], s["market_impact"],
                                                         s["original_ratio"], s["n_resolved"])
            rows.append({"author": author, "season": season, "score_window": window, "computed_at": now, **s})
    return rows


# ---- market impact ------------------------------------------------------------------
def excess_move(points: list, t: datetime, horizon: timedelta = timedelta(minutes=30)) -> float | None:
    """|P(t + 30m) - P(t)| minus the mean |30-minute change| over the 24 h before t."""
    def at(x):
        vals = [p.prob for p in points if p.t <= x and p.prob is not None]
        return vals[-1] if vals else None
    p0, p1 = at(t), at(t + horizon)
    if p0 is None or p1 is None:
        return None
    base = []
    s = t - timedelta(hours=24)
    while s + horizon <= t:
        a, b = at(s), at(s + horizon)
        if a is not None and b is not None:
            base.append(abs(b - a))
        s += horizon
    return round(abs(p1 - p0) - (statistics.mean(base) if base else 0.0), 4)


def market_moves(store, claims: list[dict], now: datetime) -> list[dict]:
    """market_move for own claims about a game, once 30 minutes have passed."""
    updates = []
    games = {g["game_id"]: g for g in store.get_games()}
    for c in claims:
        if c.get("market_move") is not None or not c.get("game_id") or c.get("credited_handle"):
            continue
        t = c["posted_at"]
        g = games.get(c["game_id"])
        if not g or t + timedelta(minutes=30) > min(now, g["kickoff_at"]):
            continue
        snaps = store.odds_series(t - timedelta(hours=24), t + timedelta(minutes=30), c["game_id"], "winner")
        series = detect_moves.build_series(snaps, {g["game_id"]: g}).get((g["game_id"], "winner"), {})
        venue = next((v for v in VENUE_ORDER if series.get(v)), None)
        if venue:
            mv = excess_move(series[venue], t)
            if mv is not None:
                updates.append({"claim_id": c["claim_id"], "market_move": mv})
                c["market_move"] = mv
    return updates


def run(store, season: int, now: datetime | None = None) -> int:
    now = now or utcnow()
    claims = store.claims(since=datetime(season, 8, 1))
    if not claims:
        return 0
    store.upsert_claims(market_moves(store, claims, now))
    rows = compute_scores(claims, now, season)
    store.upsert_insider_scores(rows)
    log.info("scored %d insiders", len({r["author"] for r in rows}))
    return len(rows)


def main() -> None:
    import nflreadpy
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--season", type=int, default=nflreadpy.get_current_season())
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    run(store, args.season)


if __name__ == "__main__":
    main()
