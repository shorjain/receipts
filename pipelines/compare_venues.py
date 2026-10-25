"""Compare Kalshi and Polymarket: price gaps and which venue moves first.

Gaps (VENUE_GAPS): the two venues' derived per-game series are aligned with an
as-of join (a value stays current for align_tolerance_minutes, longer than the
10-minute storage heartbeat). A gap is a run of at least gap_min_points aligned
points where |(b - a) - baseline| >= gap_prob_points (winner) or gap_line_points
(spread, total). The baseline is the median gap over the preceding
gap_baseline_minutes, so a standing offset between the venues isn't news.
  mover       the venue that changed more from just before the gap to its peak
  resolution  follower_caught_up (the other venue moved to the mover),
              mover_reverted (the mover came back), or open (still running / kickoff)

Lead-lag (LINE_MOVES.venue_reactions, leader_venue, lead_lag_minutes): for each
move, a venue's reaction time is the first moment, in minutes from window_start
(negative = before it), when the venue crossed the move's halfway price
(before + reaction_fraction x size) coming from the pre-move side. The earliest venue
leads; lead_lag_minutes is how far ahead of the next venue it was.

Run:  python -m pipelines.compare_venues --hours 48
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone

from common.log import get_logger
from common.settings import thresholds
from pipelines import detect_moves as dm

log = get_logger("compare_venues")


def _cfg() -> dict:
    return {**thresholds()["moves"], **thresholds()["venues"]}


def _key(market_type: str) -> str:
    return "prob" if market_type == "winner" else "line"


# ---- gaps ---------------------------------------------------------------------------
def align(a: list[dm.Point], b: list[dm.Point], key: str, tolerance_minutes: float
          ) -> list[tuple[datetime, float, float]]:
    """As-of join of two sorted series on the union of their timestamps."""
    tol = timedelta(minutes=tolerance_minutes)
    a = [p for p in a if getattr(p, key) is not None]
    b = [p for p in b if getattr(p, key) is not None]
    out, ia, ib = [], -1, -1
    for t in sorted({p.t for p in a} | {p.t for p in b}):
        while ia + 1 < len(a) and a[ia + 1].t <= t:
            ia += 1
        while ib + 1 < len(b) and b[ib + 1].t <= t:
            ib += 1
        if ia >= 0 and ib >= 0 and t - a[ia].t <= tol and t - b[ib].t <= tol:
            out.append((t, getattr(a[ia], key), getattr(b[ib], key)))
    return out


def excess_gaps(aligned: list[tuple[datetime, float, float]], baseline_minutes: float,
                min_points: int = 10) -> list[tuple[datetime, float, float, float, float]]:
    """Add (baseline, excess) to each aligned point. The baseline is the median gap
    over the preceding baseline_minutes, so a standing offset between the venues
    (fees, a stale quote) isn't mistaken for news. It's 0 until min_points of
    history exist. A long divergence eventually becomes the new baseline."""
    out, window = [], timedelta(minutes=baseline_minutes)
    for i, (t, va, vb) in enumerate(aligned):
        prior = [r[2] - r[1] for r in aligned[:i] if t - r[0] <= window]
        base = statistics.median(prior) if len(prior) >= min_points else 0.0
        out.append((t, va, vb, base, (vb - va) - base))
    return out


def gap_episodes(aligned: list[tuple[datetime, float, float]], threshold: float, min_points: int,
                 venue_a: str = "kalshi", venue_b: str = "polymarket",
                 baseline_minutes: float = 0, baseline_min_points: int = 10) -> list[dict]:
    """Runs of at least min_points where |gap - baseline| >= threshold."""
    rows = (excess_gaps(aligned, baseline_minutes, baseline_min_points) if baseline_minutes
            else [(t, va, vb, 0.0, vb - va) for t, va, vb in aligned])
    out, i, n = [], 0, len(rows)
    while i < n:
        if abs(rows[i][4]) < threshold - 1e-9:
            i += 1
            continue
        j = i
        while j + 1 < n and abs(rows[j + 1][4]) >= threshold - 1e-9:
            j += 1
        if j - i + 1 >= min_points:
            run = rows[i:j + 1]
            peak = max(run, key=lambda r: abs(r[4]))
            pre = rows[i - 1] if i > 0 else None
            post = rows[j + 1] if j + 1 < n else None
            mover = None
            if pre:
                da, db = abs(peak[1] - pre[1]), abs(peak[2] - pre[2])
                mover = venue_a if da > db else venue_b if db > da else None
            if post is None:
                resolution = "open"
            else:
                ca, cb = abs(post[1] - peak[1]), abs(post[2] - peak[2])
                closer = venue_a if ca > cb else venue_b
                resolution = ("closed" if mover is None else
                              "mover_reverted" if closer == mover else "follower_caught_up")
            out.append({
                "venue_a": venue_a, "venue_b": venue_b, "gap_start": run[0][0], "gap_end": run[-1][0],
                "peak_at": peak[0], "peak_gap": round(peak[2] - peak[1], 4), "baseline_gap": round(run[0][3], 4),
                "a_before": pre[1] if pre else None, "b_before": pre[2] if pre else None,
                "a_at_peak": peak[1], "b_at_peak": peak[2], "mover": mover, "resolution": resolution,
            })
        i = j + 1
    return out


def gap_id(game_id: str, market_type: str, start: datetime) -> str:
    return hashlib.sha1(f"gap|{game_id}|{market_type}|{start:%Y-%m-%dT%H:%M}".encode()).hexdigest()[:16]


# ---- lead-lag ---------------------------------------------------------------------
def reaction_times(move: dict, by_venue: dict[str, list[dm.Point]], cfg: dict) -> dict[str, float | None]:
    """Minutes from window_start until each venue crossed the move's halfway price.

    The halfway price comes from the move itself (before + fraction x size), shared by
    all venues. A venue only counts once it has been on the pre-move side of that
    price and then crosses it, searching from lookback_minutes before the window, so
    a venue still sitting at a previous move's level doesn't register instantly.
    Venues whose series never crosses get None.
    """
    key = _key(move["market_type"])
    before, after = move[f"{key}_before"], move[f"{key}_after"]
    if before is None or after is None or after == before:
        return {}
    sign = 1 if after > before else -1
    target = before + sign * cfg["reaction_fraction"] * abs(after - before)
    start, end = move["window_start"], move["window_end"]
    scan_from = start - timedelta(minutes=cfg["lookback_minutes"])
    horizon = end + timedelta(minutes=cfg["reaction_after_minutes"])
    out: dict[str, float | None] = {}
    for venue, pts in by_venue.items():
        valued = [p for p in pts if getattr(p, key) is not None]
        prior = [p for p in valued if p.t < scan_from][-1:]     # the price in force at scan start
        window = prior + [p for p in valued if scan_from <= p.t <= horizon]
        if not window:
            continue
        out[venue], armed = None, False
        for p in window:
            past = (getattr(p, key) - target) * sign >= -1e-9
            if not past:
                armed = True
            elif armed:
                out[venue] = round((p.t - start).total_seconds() / 60, 1)
                break
    return out


def leader(reactions: dict[str, float | None]) -> tuple[str | None, float | None]:
    timed = sorted((m, v) for v, m in reactions.items() if m is not None)
    if not timed:
        return None, None
    lag = round(timed[1][0] - timed[0][0], 1) if len(timed) > 1 else None
    return timed[0][1], lag


def summarize(moves: list[dict], gaps: list[dict], a: str = "kalshi", b: str = "polymarket") -> dict:
    """Headline numbers: who reacts first to moves, and how gaps resolve."""
    diffs = []
    for m in moves:
        r = m.get("venue_reactions") or {}
        if r.get(a) is not None and r.get(b) is not None:
            diffs.append(r[a] - r[b])  # positive = b reacted first
    out = {
        "moves_both_reacted": len(diffs),
        f"{b}_first": sum(d >= 1 for d in diffs),
        f"{a}_first": sum(d <= -1 for d in diffs),
        "same_minute": sum(-1 < d < 1 for d in diffs),
        f"median_{b}_lead_minutes": statistics.median(diffs) if diffs else None,
        "gaps": len(gaps),
    }
    for venue in (a, b):
        mine = [g for g in gaps if g["mover"] == venue]
        out[f"gaps_moved_by_{venue}"] = len(mine)
        out[f"gaps_{venue}_led_and_was_followed"] = sum(g["resolution"] == "follower_caught_up" for g in mine)
    return out


# ---- pipeline ---------------------------------------------------------------------
def run(store, hours: float = 48, now: datetime | None = None, game_id: str | None = None) -> dict:
    cfg = _cfg()
    a, b = cfg["compare"]
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(hours=hours)
    look = timedelta(minutes=cfg["lookback_minutes"])
    games = {g["game_id"]: g for g in store.games_between(start - timedelta(days=14), now + timedelta(days=14))}
    series = dm.build_series(store.odds_series(start - look, now, game_id=game_id), games)

    gap_rows = []
    for (gid, mtype), by_venue in series.items():
        if a not in by_venue or b not in by_venue:
            continue
        th = cfg["gap_prob_points"] if mtype == "winner" else cfg["gap_line_points"]
        aligned = align(by_venue[a], by_venue[b], _key(mtype), cfg["align_tolerance_minutes"])
        for e in gap_episodes(aligned, th, cfg["gap_min_points"], a, b,
                              cfg["gap_baseline_minutes"], cfg["gap_baseline_min_points"]):
            gap_rows.append({**e, "gap_id": gap_id(gid, mtype, e["gap_start"]), "game_id": gid,
                             "market_type": mtype, "detected_at": now})
    store.upsert_venue_gaps(gap_rows)

    move_rows, moves = [], store.line_moves(start, now, game_id=game_id)
    for m in moves:
        r = reaction_times(m, series.get((m["game_id"], m["market_type"]), {}), cfg)
        lead, lag = leader(r)
        m["venue_reactions"] = r
        move_rows.append({"move_id": m["move_id"], "venue_reactions": json.dumps(r),
                          "leader_venue": lead, "lead_lag_minutes": lag})
    store.upsert_line_moves(move_rows)
    summary = summarize(moves, gap_rows, a, b)
    log.info("venue comparison: %s", summary)
    return summary


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=48)
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    for k, v in run(store, args.hours).items():
        print(f"{k:>40}: {v}")


if __name__ == "__main__":
    main()
