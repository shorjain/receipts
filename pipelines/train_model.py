"""Win-probability model, walk-forward validation, and predictions (milestone 8).

Features (all known before kickoff; each game uses only games played before it):
  rolling EPA per play, offense and defense, over the team's last 4 and 8 games
  success rate (offense, defense) and QB EPA per dropback over the last 8
  rest days (nflverse schedules), away team's travel distance, neutral site
  injuries: players listed Out/Doubtful that week, weighted by their snap share in
  earlier games this season (offense and defense), plus a starting-QB-out flag
Each feature enters as home minus away. Ties are dropped.

Models: logistic regression and gradient boosting (scikit-learn). Walk-forward by
week: for every week of the test seasons, fit on all games before that week and
predict it. Scored with log loss, Brier score and a calibration table against
  - the closing line: nflverse's closing moneylines with the vig removed (the
    brief asks for Pinnacle's close; that would cost Odds API history credits, so
    nflverse's consensus close stands in), and
  - Kalshi's P(home win) one hour before kickoff, where backfilled data exists.
Beating the closing line is the real benchmark; the report says plainly if we don't.

Outputs: PREDICTIONS rows (walk-forward as "<model>-wf", live as "<model>-live" for
upcoming games, with the market price at the time), and data/model/report.json.

Run:  python -m pipelines.train_model [--test-seasons 2023 2024 2025 2026]
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from common import teams
from common.log import get_logger
from common.settings import ROOT, env_float, raw_dir

log = get_logger("train_model")

FIRST_SEASON = 2019
MODEL_DIR = ROOT / "data" / "model"
FEATURES = ["d_off_epa4", "d_off_epa8", "d_def_epa4", "d_def_epa8", "d_off_sr8", "d_def_sr8", "d_qb_epa8",
            "rest_diff", "away_travel", "neutral", "d_inj_off", "d_inj_def", "d_qb_out"]
MARKET_OFFSET = timedelta(hours=1)    # compare with the market one hour before kickoff

# Approximate home-stadium coordinates (lat, lon) for travel distance.
STADIUMS = {
    "ARI": (33.53, -112.26), "ATL": (33.76, -84.40), "BAL": (39.28, -76.62), "BUF": (42.77, -78.79),
    "CAR": (35.23, -80.85), "CHI": (41.86, -87.62), "CIN": (39.10, -84.52), "CLE": (41.51, -81.70),
    "DAL": (32.75, -97.09), "DEN": (39.74, -105.02), "DET": (42.34, -83.05), "GB": (44.50, -88.06),
    "HOU": (29.68, -95.41), "IND": (39.76, -86.16), "JAX": (30.32, -81.64), "KC": (39.05, -94.48),
    "LA": (33.95, -118.34), "LAC": (33.95, -118.34), "LV": (36.09, -115.18), "MIA": (25.96, -80.24),
    "MIN": (44.97, -93.26), "NE": (42.09, -71.26), "NO": (29.95, -90.08), "NYG": (40.81, -74.07),
    "NYJ": (40.81, -74.07), "PHI": (39.90, -75.17), "PIT": (40.45, -80.02), "SEA": (47.60, -122.33),
    "SF": (37.40, -121.97), "TB": (27.98, -82.50), "TEN": (36.17, -86.77), "WAS": (38.91, -76.86),
}


def _canon(code: str) -> str:
    try:
        return teams.canon(code)
    except KeyError:
        return code


def km(a: str, b: str) -> float:
    (la1, lo1), (la2, lo2) = STADIUMS.get(_canon(a), (0, 0)), STADIUMS.get(_canon(b), (0, 0))
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# ---- data ----------------------------------------------------------------------------
def _cache(name: str, season: int, current: int, fn) -> pd.DataFrame:
    """Past seasons don't change: compute once, keep a Parquet copy under data/raw/model."""
    path = raw_dir() / "model" / f"{name}_{season}.parquet"
    if season < current and path.exists():
        return pd.read_parquet(path)
    df = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df


def team_game_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, team): offensive and defensive EPA/play, success rate, QB EPA."""
    plays = pbp[((pbp["pass"] == 1) | (pbp["rush"] == 1)) & pbp["epa"].notna() & pbp["posteam"].notna()]
    off = plays.groupby(["game_id", "posteam"]).agg(off_epa=("epa", "mean"), off_sr=("success", "mean"),
                                                    off_plays=("epa", "size")).reset_index()
    drop = plays[plays["qb_dropback"] == 1].groupby(["game_id", "posteam"]).agg(qb_epa=("qb_epa", "mean")).reset_index()
    dfn = plays.groupby(["game_id", "defteam"]).agg(def_epa=("epa", "mean"), def_sr=("success", "mean")).reset_index()
    out = off.merge(drop, on=["game_id", "posteam"], how="left").rename(columns={"posteam": "team"})
    return out.merge(dfn.rename(columns={"defteam": "team"}), on=["game_id", "team"], how="left")


def load_season(season: int, current: int) -> dict[str, pd.DataFrame]:
    import nflreadpy as n

    def pbp_stats():
        cols = ["game_id", "posteam", "defteam", "epa", "success", "pass", "rush", "qb_dropback", "qb_epa"]
        return team_game_stats(n.load_pbp(season).select(cols).to_pandas())
    return {
        "schedule": _cache("schedule", season, current, lambda: n.load_schedules(season).to_pandas()),
        "team_games": _cache("team_games", season, current, pbp_stats),
        "injuries": _cache("injuries", season, current, lambda: n.load_injuries(season).to_pandas()),
        "snaps": _cache("snaps", season, current, lambda: n.load_snap_counts(season).to_pandas()),
    }


# ---- features ------------------------------------------------------------------------
def rolling_team_features(schedule: pd.DataFrame, team_games: pd.DataFrame) -> pd.DataFrame:
    """Per (game, team): means over that team's previous 4 / 8 played games.

    Built over every scheduled game, played or not, so an upcoming game gets the team's
    latest form; the current game is always excluded (shift by one)."""
    sides = [schedule[["game_id", "gameday", f"{s}_team"]].rename(columns={f"{s}_team": "team"})
             for s in ("home", "away")]
    long = pd.concat(sides, ignore_index=True).drop_duplicates(["game_id", "team"])
    tg = long.merge(team_games, on=["game_id", "team"], how="left").sort_values(["team", "gameday", "game_id"])
    out = tg[["game_id", "team"]].copy()
    g = tg.groupby("team")
    for col in ("off_epa", "def_epa"):
        for k in (4, 8):
            out[f"{col}{k}"] = g[col].transform(lambda s, k=k: s.shift(1).rolling(k, min_periods=2).mean())
    for col in ("off_sr", "def_sr", "qb_epa"):
        out[f"{col}8"] = g[col].transform(lambda s: s.shift(1).rolling(8, min_periods=2).mean())
    return out


def injury_features(injuries: pd.DataFrame, snaps: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    """Per (season, week, team): snap share of players listed Out/Doubtful, from earlier weeks."""
    if injuries.empty:
        return pd.DataFrame(columns=["season", "week", "team", "inj_off", "inj_def", "qb_out"])
    pfr = dict(zip(players["gsis_id"], players["pfr_id"]))
    out = injuries[injuries["report_status"].isin(["Out", "Doubtful"])].copy()
    out["pfr"] = out["gsis_id"].map(pfr)
    s = snaps[["season", "week", "pfr_player_id", "offense_pct", "defense_pct"]].copy()
    rows = []
    for r in out.itertuples(index=False):
        prior = s[(s["pfr_player_id"] == r.pfr) & (s["season"] == r.season) & (s["week"] < r.week)]
        o = prior["offense_pct"].mean() if len(prior) else 0.0
        d = prior["defense_pct"].mean() if len(prior) else 0.0
        rows.append({"season": r.season, "week": r.week, "team": _canon(r.team), "off": o or 0.0, "def": d or 0.0,
                     "qb": 1.0 if r.position == "QB" and (o or 0) >= 0.5 else 0.0})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["season", "week", "team", "inj_off", "inj_def", "qb_out"])
    return df.groupby(["season", "week", "team"]).agg(inj_off=("off", "sum"), inj_def=("def", "sum"),
                                                      qb_out=("qb", "max")).reset_index()


def no_vig_home(home_ml, away_ml) -> float | None:
    if home_ml is None or away_ml is None or pd.isna(home_ml) or pd.isna(away_ml):
        return None
    p = lambda o: 100 / (o + 100) if o > 0 else -o / (-o + 100)
    a, b = p(home_ml), p(away_ml)
    return a / (a + b)


def build_frame(seasons: dict[int, dict[str, pd.DataFrame]], players: pd.DataFrame) -> pd.DataFrame:
    sched = pd.concat([d["schedule"] for d in seasons.values()], ignore_index=True)
    sched = sched[sched["game_type"].isin(["REG", "WC", "DIV", "CON", "SB"])].copy()
    tg = pd.concat([d["team_games"] for d in seasons.values()], ignore_index=True)
    roll = rolling_team_features(sched, tg)
    inj = pd.concat([injury_features(d["injuries"], d["snaps"], players) for d in seasons.values()], ignore_index=True)
    df = sched.copy()
    for side in ("home", "away"):
        r = roll.rename(columns={c: f"{side}_{c}" for c in roll.columns if c not in ("game_id", "team")})
        df = df.merge(r, left_on=["game_id", f"{side}_team"], right_on=["game_id", "team"], how="left").drop(columns="team")
        df[f"{side}_canon"] = df[f"{side}_team"].map(_canon)
        i = inj.rename(columns={"inj_off": f"{side}_inj_off", "inj_def": f"{side}_inj_def", "qb_out": f"{side}_qb_out"})
        df = df.merge(i, left_on=["season", "week", f"{side}_canon"], right_on=["season", "week", "team"],
                      how="left").drop(columns="team")
    for k in ("inj_off", "inj_def", "qb_out"):
        df[f"home_{k}"] = df[f"home_{k}"].fillna(0.0)
        df[f"away_{k}"] = df[f"away_{k}"].fillna(0.0)
    for c in ("off_epa4", "off_epa8", "def_epa4", "def_epa8", "off_sr8", "def_sr8", "qb_epa8"):
        df[f"d_{c}"] = df[f"home_{c}"] - df[f"away_{c}"]
    df["rest_diff"] = (df["home_rest"] - df["away_rest"]).clip(-7, 7)
    df["neutral"] = (df["location"] == "Neutral").astype(float)
    df["away_travel"] = [0.0 if n else km(a, h) / 1000 for a, h, n in zip(df["away_team"], df["home_team"], df["neutral"])]
    df["d_inj_off"], df["d_inj_def"] = df["home_inj_off"] - df["away_inj_off"], df["home_inj_def"] - df["away_inj_def"]
    df["d_qb_out"] = df["home_qb_out"] - df["away_qb_out"]
    df["kickoff_at"] = pd.to_datetime(df["gameday"] + " " + df["gametime"].fillna("13:00")).dt.tz_localize(
        "America/New_York").dt.tz_convert("UTC").dt.tz_localize(None)
    df["home_win"] = np.where(df["result"] > 0, 1.0, np.where(df["result"] < 0, 0.0, np.nan))
    df["close_prob"] = [no_vig_home(h, a) for h, a in zip(df["home_moneyline"], df["away_moneyline"])]
    return df.sort_values("kickoff_at").reset_index(drop=True)


# ---- models ----------------------------------------------------------------------------
def make_model(name: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if name == "logreg":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.5, max_iter=1000))
    if name == "gbm":
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=40,
                                              l2_regularization=1.0, random_state=0)
    raise ValueError(name)


def walk_forward(df: pd.DataFrame, model_name: str, test_seasons: list[int]) -> pd.DataFrame:
    """Predict each test week from a model fit only on games played before it."""
    done = df[df["home_win"].notna()]
    preds = []
    for season in test_seasons:
        for week in sorted(done[done["season"] == season]["week"].unique()):
            test = done[(done["season"] == season) & (done["week"] == week)]
            train = done[done["kickoff_at"] < test["kickoff_at"].min()]
            if len(train) < 200 or test.empty:
                continue
            m = make_model(model_name).fit(train[FEATURES], train["home_win"])
            preds.append(test.assign(model_prob=m.predict_proba(test[FEATURES])[:, 1]))
    return pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()


def log_loss(y, p) -> float:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y, p) -> float:
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def calibration(y, p, bins: int = 10) -> list[dict]:
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.sum():
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "predicted": float(p[m].mean()), "actual": float(y[m].mean()),
                        "n": int(m.sum())})
    return out


def score(frame: pd.DataFrame, cols: dict[str, str]) -> dict:
    """Metrics for each probability column on the rows where all of them exist."""
    sub = frame.dropna(subset=list(cols.values()) + ["home_win"])
    return {"n": len(sub), **{name: {"log_loss": round(log_loss(sub["home_win"], sub[c]), 4),
                                     "brier": round(brier(sub["home_win"], sub[c]), 4),
                                     "calibration": calibration(sub["home_win"], sub[c])}
                              for name, c in cols.items()}}


# ---- market prices ---------------------------------------------------------------------
def market_prob(store, game_id: str, kickoff: datetime, at: datetime) -> float | None:
    """P(home win) as of `at` from Kalshi, else Polymarket, else the book consensus."""
    from pipelines import detect_moves
    snaps = store.odds_series(at - timedelta(hours=12), at, game_id=game_id, market_type="winner")
    g = {game_id: {"game_id": game_id, "kickoff_at": kickoff}}
    series = detect_moves.build_series(snaps, g).get((game_id, "winner"), {})
    for venue in ("kalshi", "polymarket", "consensus", "pinnacle"):
        pts = [p for p in series.get(venue, []) if p.t <= at and p.prob is not None]
        if pts:
            return pts[-1].prob
    return None


# ---- pipeline --------------------------------------------------------------------------
def run(store, test_seasons: list[int] | None = None, now: datetime | None = None) -> dict:
    import nflreadpy as n
    from sources import nflverse
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    current = n.get_current_season()
    test_seasons = test_seasons or [current - 3, current - 2, current - 1, current]
    seasons = {s: load_season(s, current) for s in range(FIRST_SEASON, current + 1)}
    players = nflverse.load_players(save=False)
    df = build_frame(seasons, players)
    log.info("model frame: %d games, %d with results", len(df), int(df["home_win"].notna().sum()))

    report = {"generated_at": now.isoformat(), "test_seasons": test_seasons, "features": FEATURES, "models": {}}
    wf_all = {}
    for name in ("logreg", "gbm"):
        wf = walk_forward(df, name, test_seasons)
        wf_all[name] = wf
        report["models"][name] = {"vs_close": score(wf, {"model": "model_prob", "close": "close_prob"})}
    # Kalshi comparison: games with a backfilled/live Kalshi price an hour before kickoff.
    best = min(report["models"], key=lambda k: report["models"][k]["vs_close"]["model"]["log_loss"])
    wf = wf_all[best]
    cur = wf[wf["season"] == current].copy()
    cur["kalshi_prob"] = [market_prob(store, gid, ko, ko - MARKET_OFFSET) for gid, ko in zip(cur["game_id"], cur["kickoff_at"])]
    report["best_model"] = best
    report["models"][best]["vs_kalshi"] = score(cur, {"model": "model_prob", "kalshi": "kalshi_prob", "close": "close_prob"})

    rows = [{"game_id": r.game_id, "model_version": f"{best}-wf", "made_at": r.kickoff_at - MARKET_OFFSET,
             "home_win_prob": float(r.model_prob), "market_prob_at_time": None if pd.isna(r.close_prob) else float(r.close_prob),
             "features_json": {f: (None if pd.isna(getattr(r, f)) else float(getattr(r, f))) for f in FEATURES}}
            for r in wf.itertuples(index=False)]

    # Live predictions for upcoming games, from a model fit on every finished game.
    done = df[df["home_win"].notna()]
    final = make_model(best).fit(done[FEATURES], done["home_win"])
    upcoming = df[(df["kickoff_at"] > now) & (df["kickoff_at"] <= now + timedelta(days=8))]
    contrib = {}
    if best == "logreg" and len(upcoming):
        lr, scaler = final[-1], final[1]
        z = scaler.transform(final[0].transform(upcoming[FEATURES]))
        contrib = {gid: dict(zip(FEATURES, (z[i] * lr.coef_[0]).round(3)))
                   for i, gid in enumerate(upcoming["game_id"])}
    for r, p in zip(upcoming.itertuples(index=False), final.predict_proba(upcoming[FEATURES])[:, 1] if len(upcoming) else []):
        feats = {f: (None if pd.isna(getattr(r, f)) else float(getattr(r, f))) for f in FEATURES}
        if r.game_id in contrib:
            feats["_contributions"] = {k: float(v) for k, v in contrib[r.game_id].items()}
        rows.append({"game_id": r.game_id, "model_version": f"{best}-live", "made_at": now, "home_win_prob": float(p),
                     "market_prob_at_time": market_prob(store, r.game_id, r.kickoff_at, now), "features_json": feats})
    store.upsert_predictions(rows)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "report.json").write_text(json.dumps(report, indent=1, default=str))
    m = report["models"][best]["vs_close"]
    log.info("best model %s: log loss %.4f vs close %.4f (n=%d); %d live predictions", best,
             m["model"]["log_loss"], m["close"]["log_loss"], m["n"], len(upcoming))
    return report


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test-seasons", type=int, nargs="*")
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    rep = run(store, args.test_seasons)
    for name, m in rep["models"].items():
        for bench, sc in m.items():
            print(name, bench, {k: (v["log_loss"], v["brier"]) if isinstance(v, dict) else v for k, v in sc.items()})


if __name__ == "__main__":
    main()
