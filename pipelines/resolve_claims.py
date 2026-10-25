"""Check claims against what actually happened, using nflverse (Feature B, step 2).

Rules (outcome: correct | incorrect | unresolvable; claims whose data isn't in yet
stay pending and are retried):
  active / questionable_will_play   correct if the player took a snap in that game
  inactive                          correct if the player took no snap in that game
                                    (snap_counts, via the gsis -> pfr ID crosswalk)
  starter     checked only after the game is played (snap counts published). QBs: the
              passer on the team's first pass play (pbp). Others: played at least half
              of the team's offensive or defensive snaps. That's a stand-in, because
              2026 play-by-play participation (the real starting lineups) isn't
              published, and depth charts lag injuries (a backup QB named the starter
              after the starter is ruled out is still listed second).
  trade / signing / release   weekly rosters after the post, up to resolve_by:
              trade = the player's team changed (to the named team, if one was named);
              signing = he appears on the named team; release = he's cut or off the team.
  return_date the first week he records a snap equals the predicted week.
  other, or no matched player       unresolvable (doesn't count toward accuracy).
A claim whose data never arrives becomes unresolvable GRACE after resolve_by.

Run:  python -m pipelines.resolve_claims [--season 2026]
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from common.log import get_logger
from pipelines.extract_claims import norm_team
from pipelines.search_x import utcnow

log = get_logger("resolve_claims")
GRACE = timedelta(days=7)
ON_ROSTER = {"ACT", "RES", "INA", "DEV", "EXE"}


@dataclass
class Context:
    games: dict[str, dict] = field(default_factory=dict)
    snaps: dict[tuple[str, str], float] = field(default_factory=dict)        # (game_id, pfr) -> snaps
    snap_pct: dict[tuple[str, str], float] = field(default_factory=dict)     # (game_id, pfr) -> max(off%, def%)
    snap_games: set[str] = field(default_factory=set)
    snap_weeks: dict[str, dict[int, float]] = field(default_factory=dict)    # pfr -> week -> snaps
    last_snap_week: int = 0
    pfr_of: dict[str, str] = field(default_factory=dict)                     # gsis -> pfr
    position: dict[str, str] = field(default_factory=dict)                   # gsis -> position
    roster: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)  # gsis -> [(week, team, status)]
    last_roster_week: int = 0
    first_passer: dict[tuple[str, str], str] = field(default_factory=dict)   # (game_id, team) -> gsis
    week_start: dict[int, datetime] = field(default_factory=dict)            # week -> first kickoff


def build_context(games: list[dict], snaps: pd.DataFrame, players: pd.DataFrame, rosters: pd.DataFrame,
                  pbp: pd.DataFrame | None = None) -> Context:
    ctx = Context(games={g["game_id"]: g for g in games})
    for g in games:
        ws = ctx.week_start.get(g["week"])
        ctx.week_start[g["week"]] = min(ws, g["kickoff_at"]) if ws else g["kickoff_at"]
    s = snaps.copy()
    s["total"] = s[["offense_snaps", "defense_snaps", "st_snaps"]].fillna(0).sum(axis=1)
    s["pct"] = s[["offense_pct", "defense_pct"]].fillna(0).max(axis=1)
    for r in s[["game_id", "pfr_player_id", "week", "total", "pct"]].itertuples(index=False):
        ctx.snaps[(r.game_id, r.pfr_player_id)] = r.total
        ctx.snap_pct[(r.game_id, r.pfr_player_id)] = r.pct
        ctx.snap_weeks.setdefault(r.pfr_player_id, {})[int(r.week)] = r.total
    ctx.snap_games = set(s["game_id"].unique())
    ctx.last_snap_week = int(s["week"].max()) if len(s) else 0
    p = players[players["gsis_id"].notna()]
    ctx.pfr_of = dict(zip(p["gsis_id"], p["pfr_id"]))
    ctx.position = dict(zip(p["gsis_id"], p.get("position", pd.Series(dtype=str))))
    for r in rosters[rosters["gsis_id"].notna()][["gsis_id", "week", "team", "status"]].itertuples(index=False):
        ctx.roster.setdefault(r.gsis_id, []).append((int(r.week), r.team, r.status))
    for v in ctx.roster.values():
        v.sort()
    ctx.last_roster_week = int(rosters["week"].max()) if len(rosters) else 0
    if pbp is not None and len(pbp):
        passes = pbp[(pbp["pass_attempt"] == 1) & pbp["passer_player_id"].notna()].sort_values(["game_id", "play_id"])
        for (gid, team), grp in passes.groupby(["game_id", "posteam"]):
            ctx.first_passer[(gid, team)] = grp.iloc[0]["passer_player_id"]
    return ctx


def week_of(ctx: Context, t: datetime) -> int:
    """The NFL week a moment belongs to: the latest week whose first game started by t."""
    started = [w for w, k in ctx.week_start.items() if k <= t]
    return max(started) if started else min(ctx.week_start, default=1)


def _pending_or(ctx_now: datetime, claim: dict, reason: str):
    rb = claim.get("resolve_by")
    if rb and ctx_now > rb + GRACE:
        return "unresolvable", {"reason": reason}
    return None, {"reason": reason}


def resolve(claim: dict, ctx: Context, now: datetime) -> tuple[str | None, dict]:
    """(outcome or None if still pending, evidence)."""
    ctype, gsis = claim["claim_type"], claim.get("player_id")
    if ctype == "other" or not gsis:
        return "unresolvable", {"reason": "no checkable data" if gsis else "player not matched to an nflverse ID"}
    pfr = ctx.pfr_of.get(gsis)

    if ctype in ("active", "inactive", "questionable_will_play"):
        gid = claim.get("game_id")
        if not gid:
            return "unresolvable", {"reason": "no game found for the player's team"}
        if gid not in ctx.snap_games:
            return _pending_or(now, claim, "snap counts not published for the game yet")
        if not pfr:
            return "unresolvable", {"reason": "no PFR ID for the player"}
        snaps = ctx.snaps.get((gid, pfr), 0.0)
        played = snaps > 0
        ok = played if ctype != "inactive" else not played
        return ("correct" if ok else "incorrect"), {"table": "snap_counts", "game_id": gid,
                                                    "pfr_player_id": pfr, "snaps": snaps}

    if ctype == "starter":
        gid = claim.get("game_id")
        if not gid or gid not in ctx.games:
            return "unresolvable", {"reason": "no game found for the player's team"}
        if gid not in ctx.snap_games:
            return _pending_or(now, claim, "game not played or snap counts not published yet")
        team = claim.get("team")
        if ctx.position.get(gsis) == "QB" and (gid, team) in ctx.first_passer:
            first = ctx.first_passer[(gid, team)]
            return ("correct" if first == gsis else "incorrect"), {"table": "pbp", "game_id": gid,
                                                                   "first_passer": first}
        if not pfr:
            return "unresolvable", {"reason": "no PFR ID for the player"}
        pct = ctx.snap_pct.get((gid, pfr), 0.0)
        return ("correct" if pct >= 0.5 else "incorrect"), {"table": "snap_counts", "game_id": gid,
                                                            "pfr_player_id": pfr, "snap_share": pct,
                                                            "rule": "starter = at least 50% of snaps"}

    if ctype in ("trade", "signing", "release"):
        entries = ctx.roster.get(gsis, [])
        post_week = week_of(ctx, claim["posted_at"])
        deadline_week = week_of(ctx, claim["resolve_by"]) if claim.get("resolve_by") else post_week + 1
        before = [e for e in entries if e[0] <= post_week]
        after = [e for e in entries if post_week < e[0] <= deadline_week]
        before_team = before[-1][1] if before else claim.get("team")
        named = norm_team(re.sub(r"^(to|with)\s+", "", (claim.get("predicted_value") or "").strip(), flags=re.I))
        ev = {"table": "rosters_weekly", "before": before[-1:] or None, "after": after}
        if ctype == "trade":
            hit = [e for e in after if e[1] != before_team and (not named or e[1] == named)]
        elif ctype == "signing":
            target = named or claim.get("team")
            hit = [e for e in after if e[1] == target and e[2] in ON_ROSTER]
        else:
            hit = [e for e in after if e[2] == "CUT" or e[1] != before_team]
        if hit:
            return "correct", ev
        if ctx.last_roster_week >= deadline_week:
            return "incorrect", ev
        return _pending_or(now, claim, "rosters not published through resolve_by yet")

    if ctype == "return_date":
        m = re.search(r"week\s*(\d+)", claim.get("predicted_value") or "", re.I)
        if not m or not pfr:
            return "unresolvable", {"reason": "no week in the claim" if not m else "no PFR ID for the player"}
        predicted, post_week = int(m.group(1)), week_of(ctx, claim["posted_at"])
        played = sorted(w for w, s in ctx.snap_weeks.get(pfr, {}).items() if w > post_week and s > 0)
        ev = {"table": "snap_counts", "pfr_player_id": pfr, "predicted_week": predicted,
              "first_week_with_snaps": played[0] if played else None}
        if played and played[0] <= ctx.last_snap_week:
            return ("correct" if played[0] == predicted else "incorrect"), ev
        if ctx.last_snap_week >= predicted:
            return "incorrect", ev
        return _pending_or(now, claim, "season hasn't reached the predicted week")

    return "unresolvable", {"reason": f"unknown claim type {ctype}"}


def load_context(store, season: int) -> Context:
    from sources import nflverse
    games = store.get_games(season)
    snaps = nflverse.load_snap_counts(season)
    players = nflverse.load_players()
    rosters = nflverse.load_rosters_weekly(season)
    try:
        pbp = nflverse.load_pbp(season)
    except Exception as e:  # noqa: BLE001
        log.warning("play-by-play unavailable: %s", e)
        pbp = None
    return build_context(games, snaps, players, rosters, pbp)


def run(store, season: int, now: datetime | None = None, ctx: Context | None = None) -> dict[str, int]:
    now = now or utcnow()
    open_claims = [c for c in store.claims() if c.get("outcome") is None]
    if not open_claims:
        return {}
    ctx = ctx or load_context(store, season)
    rows, counts = [], {}
    for c in open_claims:
        outcome, evidence = resolve(c, ctx, now)
        counts[outcome or "pending"] = counts.get(outcome or "pending", 0) + 1
        if outcome:
            rows.append({"claim_id": c["claim_id"], "outcome": outcome, "evidence": evidence, "resolved_at": now})
    store.upsert_resolutions(rows)
    log.info("resolved claims: %s", counts)
    return counts


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
