"""Shared math for prediction-market venues (Kalshi, Polymarket): quotes, liquidity,
and turning a ladder of binary markets into one implied line per game.

Derived lines carry the rungs they came from. Like shipyard-rs's strike_overlap check,
detection won't compare two lines whose rung sets barely overlap: when liquid rungs
appear or vanish, the interpolated line can jump without the market moving."""
from __future__ import annotations


def is_liquid(q: dict, max_spread: float, min_volume: float) -> bool:
    if q.get("bid") is None or q.get("ask") is None or q["bid"] <= 0:
        return False
    return round(q["ask"] - q["bid"], 4) < max_spread and (q.get("volume") or 0) >= min_volume


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


def derive(g: dict, mtype: str, items: list[tuple[dict, str]], max_spread: float,
           min_volume: float) -> dict | None:
    """One snapshot per game and market type from a venue's quotes.

    items are (quote, side). Quotes have bid/ask/mid/price/volume/strike.
      winner: side is a team; returns P(home wins), normalized over both teams.
      spread: side is the team whose margin must exceed strike; returns the home
              spread in betting convention (home -3.5 = home favored by 3.5).
      total:  side is "over"; returns the median total.
    """
    if mtype == "winner":
        home = next((q for q, s in items if s == g["home"]), None)
        away = next((q for q, s in items if s == g["away"]), None)
        if not home or home["mid"] is None:
            return None
        p = home["mid"]
        if away and away["mid"] is not None and home["mid"] + away["mid"] > 0:
            p = home["mid"] / (home["mid"] + away["mid"])
        return {"implied_prob": p, "price": home["price"], "line": None, "bid": home["bid"],
                "ask": home["ask"], "volume": home["volume"]}
    liquid = [(q, s) for q, s in items if is_liquid(q, max_spread, min_volume)]
    pts, rungs = [], []
    for q, side in liquid:
        if q["strike"] is None or q["mid"] is None:
            continue
        if mtype == "total":
            pts.append((q["strike"], q["mid"]))
        elif side == g["home"]:
            pts.append((q["strike"], q["mid"]))          # P(home margin > s)
        elif side == g["away"]:
            pts.append((-q["strike"], 1 - q["mid"]))     # P(home margin > -s)
        else:
            continue
        rungs.append(pts[-1][0])
    median = implied_median(pts)
    if median is None:
        return None
    # Quote of the rung nearest 50% stands in for the market's bid/ask.
    near = min(liquid, key=lambda qs: abs((qs[0]["mid"] or 0) - 0.5))[0]
    line = round(median, 2) if mtype == "total" else round(-median, 2)
    return {"implied_prob": None, "price": None, "line": line, "bid": near["bid"],
            "ask": near["ask"], "volume": sum(q["volume"] or 0 for q, _ in liquid),
            "rungs": ",".join(f"{x:g}" for x in sorted(rungs))}


def rung_overlap(a: str | None, b: str | None) -> float | None:
    """Jaccard overlap of two rung sets (as shipyard-rs's strike_overlap); None if unknown."""
    if not a or not b:
        return None
    sa, sb = set(a.split(",")), set(b.split(","))
    return len(sa & sb) / len(sa | sb)
