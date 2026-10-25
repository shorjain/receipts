"""nflverse loaders via nflreadpy (the maintained successor to nfl_data_py, which
was archived in September 2025). Every pull is also written to data/raw/nflverse/."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from common.raw import write_raw

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _nfl():
    import nflreadpy  # imported lazily so tests with fixtures don't need network deps
    return nflreadpy


def _pull(name: str, fn, season: int | list[int], save: bool) -> pd.DataFrame:
    df = fn(season).to_pandas()
    if save:
        tag = season if isinstance(season, int) else f"{min(season)}-{max(season)}"
        write_raw("nflverse", df, name=f"{name}_{tag}")
    return df


def kickoff_utc(gameday: str, gametime: str | None) -> datetime:
    """nflverse gameday/gametime are US Eastern; return naive UTC."""
    hhmm = gametime or "13:00"
    local = datetime.strptime(f"{gameday} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return local.astimezone(UTC).replace(tzinfo=None)


def _int(v):
    return None if v is None or pd.isna(v) else int(v)


def _float(v):
    return None if v is None or pd.isna(v) else float(v)


def schedules_to_games(df: pd.DataFrame) -> list[dict]:
    """Map nflverse schedule rows to GAMES rows.

    nflverse spread_line is points the home team is favored by (positive = home
    favored). We store every spread in betting convention from the home side
    (home -3.5 = home favored by 3.5), so close_spread_line = -spread_line.
    """
    rows = []
    for r in df.to_dict("records"):
        rows.append({
            "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
            "game_type": r["game_type"], "home": r["home_team"], "away": r["away_team"],
            "kickoff_at": kickoff_utc(r["gameday"], r.get("gametime")),
            "home_score": _int(r.get("home_score")), "away_score": _int(r.get("away_score")),
            "close_spread_line": None if _float(r.get("spread_line")) is None else -_float(r.get("spread_line")),
            "close_total_line": _float(r.get("total_line")),
            "roof": r.get("roof"), "home_rest": _int(r.get("home_rest")),
            "away_rest": _int(r.get("away_rest")),
            "div_game": None if pd.isna(r.get("div_game")) else bool(r.get("div_game")),
        })
    return rows


def rosters_to_rows(df: pd.DataFrame) -> list[dict]:
    df = df[df["gsis_id"].notna()]
    return [{
        "season": int(r["season"]), "week": int(r["week"]), "team": r["team"],
        "player_id": r["gsis_id"], "full_name": r["full_name"], "position": r["position"],
        "status": r["status"],
    } for r in df.to_dict("records")]


def load_schedules(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("schedules", _nfl().load_schedules, season, save)


def load_rosters_weekly(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("rosters_weekly", _nfl().load_rosters_weekly, season, save)


def load_snap_counts(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("snap_counts", _nfl().load_snap_counts, season, save)


def load_injuries(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("injuries", _nfl().load_injuries, season, save)


def load_depth_charts(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("depth_charts", _nfl().load_depth_charts, season, save)


def load_pbp(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("pbp", _nfl().load_pbp, season, save)


def load_participation(season: int, save: bool = True) -> pd.DataFrame:
    return _pull("participation", _nfl().load_participation, season, save)


def load_trades(season: int, save: bool = True) -> pd.DataFrame:
    """nflreadpy's load_trades takes no season; filter after loading."""
    df = _nfl().load_trades().to_pandas()
    df = df[df["season"] == season] if "season" in df.columns else df
    if save:
        write_raw("nflverse", df, name=f"trades_{season}")
    return df
