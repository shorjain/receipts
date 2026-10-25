"""Detect line moves from stored snapshots and write LINE_MOVES.

A move is any of (thresholds in config/thresholds.yaml):
  winner: implied home win probability changes >= prob_points within lookback_minutes
  spread: the home spread touches or passes a key number (3, 7, 10, 14) within the
          lookback, with at least spread_min_change points of movement
  total:  the total moves >= total_points within the lookback

Only pre-kickoff snapshots count. Kalshi points must be liquid (bid-ask spread under
KALSHI_MAX_SPREAD and 24h volume at least KALSHI_MIN_VOLUME). Overlapping windows for
the same game, market type and direction merge across venues; a merged move is
"multi" when two or more venues are in it.

magnitude_score is the observed change divided by its threshold (1.0 = just met it):
  winner |dp| / prob_points, total |dline| / total_points,
  spread max(|dline| / spread_points, 1.0 when a key number was crossed).

Run:  python -m pipelines.detect_moves --hours 48
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common import teams
from common.log import get_logger
from common.settings import env_float, thresholds

log = get_logger("detect_moves")

DETECT_VENUES = ("kalshi", "pinnacle", "consensus", "polymarket")


@dataclass
class Point:
    t: datetime
    prob: float | None
    line: float | None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None


@dataclass
class Candidate:
    game_id: str
    market_type: str
    venue: str
    start: datetime
    end: datetime
    before: float
    after: float
    magnitude: float
    crossed_key: int | None = None

    @property
    def direction(self) -> int:
        return (self.after > self.before) - (self.after < self.before)


@dataclass
class Move:
    game_id: str
    market_type: str
    start: datetime
    end: datetime
    candidates: list[Candidate] = field(default_factory=list)


def _cfg() -> dict:
    c = dict(thresholds()["moves"])
    c.setdefault("spread_min_change", 0.5)
    return c


def crossed_keys(a: float, b: float, keys: list[int]) -> list[int]:
    """Key numbers the spread touched or passed moving from a to b (signed lines)."""
    if a == b:
        return []
    lo, hi = min(a, b), max(a, b)
    return sorted({k for k in keys for s in (k, -k) if lo <= s <= hi})


# Prediction-market venues: only their derived per-game series count, and each point
# must be liquid. Sportsbook venues have no bid/ask to check.
EXCHANGE_VENUES = ("kalshi", "polymarket")


def liquidity_rules() -> dict[str, tuple[float, float]]:
    """venue -> (max bid-ask spread, min 24h volume)."""
    return {
        "kalshi": (env_float("KALSHI_MAX_SPREAD", 0.04), env_float("KALSHI_MIN_VOLUME", 500)),
        "polymarket": (env_float("POLYMARKET_MAX_SPREAD", 0.04), env_float("POLYMARKET_MIN_VOLUME", 500)),
    }


def is_liquid(p: Point, max_spread: float, min_volume: float) -> bool:
    if p.bid is None or p.ask is None or p.bid <= 0:
        return False
    return round(p.ask - p.bid, 4) < max_spread and (p.volume or 0) >= min_volume


def find_candidates(game_id: str, market_type: str, venue: str, points: list[Point],
                    cfg: dict | None = None) -> list[Candidate]:
    """Scan one venue's time series for threshold breaches within the lookback window."""
    cfg = cfg or _cfg()
    look = timedelta(minutes=cfg["lookback_minutes"])
    key = "prob" if market_type == "winner" else "line"
    pts = sorted((p for p in points if getattr(p, key) is not None), key=lambda p: p.t)
    out: list[Candidate] = []
    for i, cur in enumerate(pts):
        window = [p for p in pts[:i] if cur.t - p.t <= look]
        if not window:
            continue
        ref = max(window, key=lambda p: abs(getattr(cur, key) - getattr(p, key)))
        a, b = getattr(ref, key), getattr(cur, key)
        delta = abs(b - a)
        cand = None
        if market_type == "winner" and delta >= cfg["prob_points"] - 1e-9:
            cand = Candidate(game_id, market_type, venue, ref.t, cur.t, a, b, delta / cfg["prob_points"])
        elif market_type == "spread" and delta >= cfg["spread_min_change"] - 1e-9:
            keys = crossed_keys(a, b, cfg["key_numbers"])
            if keys:
                cand = Candidate(game_id, market_type, venue, ref.t, cur.t, a, b,
                                 max(delta / cfg["spread_points"], 1.0), max(keys))
        elif market_type == "total" and delta >= cfg["total_points"] - 1e-9:
            cand = Candidate(game_id, market_type, venue, ref.t, cur.t, a, b, delta / cfg["total_points"])
        if cand:
            out.append(cand)
    return out


def merge_candidates(cands: list[Candidate], gap_minutes: float = 0) -> list[Move]:
    """Merge overlapping same-direction windows per (game, market type) across venues.

    A spike and its reversal stay two moves instead of collapsing into one "+0.0" move.
    """
    gap = timedelta(minutes=gap_minutes)
    moves: list[Move] = []
    groups: dict[tuple[str, str, int], list[Candidate]] = {}
    for c in cands:
        groups.setdefault((c.game_id, c.market_type, c.direction), []).append(c)
    for (gid, mtype, _), cs in groups.items():
        cs.sort(key=lambda c: c.start)
        cur: Move | None = None
        for c in cs:
            if cur and c.start <= cur.end + gap:
                cur.end = max(cur.end, c.end)
                cur.candidates.append(c)
            else:
                cur = Move(gid, mtype, c.start, c.end, [c])
                moves.append(cur)
    return moves


def _value_at(points: list[Point], t: datetime, key: str) -> float | None:
    vals = [getattr(p, key) for p in points if p.t <= t and getattr(p, key) is not None]
    return vals[-1] if vals else None


def summarize(move: Move, series: dict[str, list[Point]], game: dict | None) -> dict:
    """Build the LINE_MOVES row for a merged move."""
    primary = max(move.candidates, key=lambda c: c.magnitude)
    agree = sorted({c.venue for c in move.candidates})
    key = "prob" if move.market_type == "winner" else "line"
    pts = series.get(primary.venue, [])
    before = _value_at(pts, move.start, key)
    after = _value_at(pts, move.end, key)
    before = primary.before if before is None else before
    after = primary.after if after is None else after
    keys = [c.crossed_key for c in move.candidates if c.crossed_key]
    row = {
        "game_id": move.game_id, "market_type": move.market_type,
        "venue_scope": "multi" if len(agree) >= 2 else "single", "venues": ",".join(agree),
        "window_start": move.start, "window_end": move.end,
        "prob_before": before if key == "prob" else None, "prob_after": after if key == "prob" else None,
        "line_before": before if key == "line" else None, "line_after": after if key == "line" else None,
        "crossed_key_number": max(keys) if keys else None,
        "magnitude_score": round(max(c.magnitude for c in move.candidates), 3),
    }
    row["summary_text"] = summary_text(row, game)
    return row


def summary_text(row: dict, game: dict | None) -> str:
    if game:
        home, away = teams.TEAMS[game["home"]][0], teams.TEAMS[game["away"]][0]
        matchup = f"{away} at {home}"
    else:
        matchup, home = row["game_id"], "home"
    when = f"{row['window_start']:%Y-%m-%d %H:%M}-{row['window_end']:%H:%M} UTC"
    venues = row["venues"]
    if row["market_type"] == "winner":
        b, a = row["prob_before"], row["prob_after"]
        what = (f"{home} win probability {b:.1%} -> {a:.1%} ({(a - b) * 100:+.1f} pp)")
    elif row["market_type"] == "spread":
        b, a = row["line_before"], row["line_after"]
        what = f"{home} spread {b:+.1f} -> {a:+.1f}"
        if row["crossed_key_number"]:
            what += f", crossing key number {row['crossed_key_number']}"
    else:
        b, a = row["line_before"], row["line_after"]
        what = f"total {b:.1f} -> {a:.1f} ({a - b:+.1f})"
    return f"{matchup}: {what}, {when}, {row['venue_scope']}-venue ({venues})."


def move_id(game_id: str, market_type: str, start: datetime) -> str:
    return hashlib.sha1(f"{game_id}|{market_type}|{start:%Y-%m-%dT%H:%M}".encode()).hexdigest()[:16]


def build_series(snapshots: list[dict], games: dict[str, dict],
                 liquidity: dict[str, tuple[float, float]] | None = None
                 ) -> dict[tuple[str, str], dict[str, list[Point]]]:
    """Group pre-kickoff, liquid snapshots into {(game, market_type): {venue: [Point]}}."""
    liquidity = liquidity_rules() if liquidity is None else liquidity
    series: dict[tuple[str, str], dict[str, list[Point]]] = {}
    for s in snapshots:
        venue = s["venue"]
        if venue not in DETECT_VENUES:
            continue
        if venue in EXCHANGE_VENUES and not str(s.get("source", "")).endswith("_derived"):
            continue
        g = games.get(s["game_id"])
        if g and s["taken_at"] >= g["kickoff_at"]:
            continue  # in-game odds are not news-driven moves
        p = Point(s["taken_at"], s.get("implied_prob"), s.get("line"),
                  s.get("bid"), s.get("ask"), s.get("volume"))
        if venue in liquidity and not is_liquid(p, *liquidity[venue]):
            continue
        series.setdefault((s["game_id"], s["market_type"]), {}).setdefault(venue, []).append(p)
    for by_venue in series.values():
        for pts in by_venue.values():
            pts.sort(key=lambda p: p.t)
    return series


def detect(snapshots: list[dict], games: dict[str, dict], cfg: dict | None = None,
           liquidity: dict[str, tuple[float, float]] | None = None) -> list[dict]:
    """Pure detection: snapshot rows (joined with MARKETS) -> LINE_MOVES rows (no ids)."""
    cfg = cfg or _cfg()
    series = build_series(snapshots, games, liquidity)
    rows = []
    for (gid, mtype), by_venue in series.items():
        cands = []
        for venue, pts in by_venue.items():
            cands += find_candidates(gid, mtype, venue, pts, cfg)
        for mv in merge_candidates(cands, cfg.get("merge_gap_minutes", 0)):
            rows.append(summarize(mv, by_venue, games.get(gid)))
    return rows


def assign_ids(rows: list[dict], existing: list[dict], games: dict[str, dict] | None = None) -> list[dict]:
    """Reuse the id of an existing overlapping move so re-runs don't create duplicates
    when a window grows; new moves get a deterministic id and status 'pending'."""
    out = []
    for r in rows:
        match = next((e for e in existing
                      if e["game_id"] == r["game_id"] and e["market_type"] == r["market_type"]
                      and e["window_start"] <= r["window_end"] and r["window_start"] <= e["window_end"]),
                     None)
        if match:
            r = {**r, "move_id": match["move_id"], "explain_status": match.get("explain_status"),
                 "window_start": min(r["window_start"], match["window_start"])}
            r["summary_text"] = summary_text(r, (games or {}).get(r["game_id"]))
        else:
            r = {**r, "move_id": move_id(r["game_id"], r["market_type"], r["window_start"]),
                 "explain_status": "pending"}
        out.append(r)
    return out


def run(store, hours: float = 48, now: datetime | None = None, game_id: str | None = None) -> list[dict]:
    """Detect moves in (now - hours, now]; game_id limits it to one game (backfill)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(hours=hours)
    look = timedelta(minutes=_cfg()["lookback_minutes"])
    snaps = store.odds_series(start - look, now, game_id=game_id)
    games = {g["game_id"]: g for g in store.games_between(start - timedelta(days=14), now + timedelta(days=14))}
    rows = detect(snaps, games)
    rows = assign_ids(rows, store.line_moves(start - look, now, game_id=game_id), games)
    for r in rows:
        r["detected_at"] = now
    store.upsert_line_moves(rows)
    log.info("detected %d moves (%d multi-venue)", len(rows), sum(r["venue_scope"] == "multi" for r in rows))
    return rows


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=48, help="look back this many hours")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    run(store, args.hours)


if __name__ == "__main__":
    main()
