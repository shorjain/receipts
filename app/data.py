"""Read-only data access for the Streamlit pages (through store/, never raw SQL)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from pipelines import detect_moves
from store import get_read_store, snapshot_path


def store():
    return get_read_store()


def snapshot_age_minutes() -> float | None:
    p = snapshot_path()
    if not p.exists():
        return None
    return (datetime.now().timestamp() - p.stat().st_mtime) / 60


@st.cache_data(ttl=60)
def games() -> pd.DataFrame:
    s = store()
    try:
        return pd.DataFrame(s.get_games())
    finally:
        s.close()


@st.cache_data(ttl=60)
def moves(days: int) -> pd.DataFrame:
    s = store()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return pd.DataFrame(s.line_moves(now - timedelta(days=days), now))
    finally:
        s.close()


@st.cache_data(ttl=60)
def move_posts(move_id: str) -> pd.DataFrame:
    s = store()
    try:
        return pd.DataFrame(s.move_posts(move_id))
    finally:
        s.close()


@st.cache_data(ttl=60)
def series(game_id: str, market_type: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Each detection venue's series around a move (same filters detection used)."""
    s = store()
    try:
        snaps = s.odds_series(start, end, game_id=game_id, market_type=market_type)
        g = {x["game_id"]: x for x in s.get_games() if x["game_id"] == game_id}
    finally:
        s.close()
    by_venue = detect_moves.build_series(snaps, g).get((game_id, market_type), {})
    key = "prob" if market_type == "winner" else "line"
    rows = [{"venue": v, "t": p.t, "value": getattr(p, key)} for v, pts in by_venue.items() for p in pts]
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def claims() -> pd.DataFrame:
    """Claims with missing text values as None, not NaN: NaN is truthy, which would make
    every claim look like it credits someone."""
    s = store()
    try:
        df = pd.DataFrame(s.claims())
    finally:
        s.close()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].where(df[col].notna(), None)
    return df


def insider_meta() -> dict[str, dict]:
    from common.settings import insiders
    return {r["handle"]: r for r in insiders()}


BADGES = {"Most accurate": "🎯", "Fastest": "⚡", "Moves markets": "📈", "Mostly reposts": "🔁"}


def badges(scores: pd.DataFrame) -> dict[str, list[str]]:
    """author -> badge labels. Minimum samples keep one lucky claim from earning a badge."""
    out: dict[str, list[str]] = {}

    def give(df, col, label, ascending=False):
        if len(df):
            a = df.sort_values(col, ascending=ascending).iloc[0]["author"]
            out.setdefault(a, []).append(f"{BADGES[label]} {label}")
    give(scores[scores["n_resolved"] >= 5], "accuracy", "Most accurate")
    give(scores[scores["median_lead_minutes"].notna()], "median_lead_minutes", "Fastest")
    give(scores[scores["market_impact"].notna() & (scores["market_impact"] > 0)], "market_impact", "Moves markets")
    for a in scores[(scores["n_claims"] >= 5) & (scores["original_ratio"] < 0.4)]["author"]:
        out.setdefault(a, []).append(f"{BADGES['Mostly reposts']} Mostly reposts")
    return out


@st.cache_data(ttl=60)
def predictions(suffix: str) -> pd.DataFrame:
    s = store()
    try:
        return pd.DataFrame(s.predictions(suffix=suffix))
    finally:
        s.close()


def model_report() -> dict | None:
    import json
    from common.settings import ROOT
    p = ROOT / "data" / "model" / "report.json"
    return json.loads(p.read_text()) if p.exists() else None


@st.cache_data(ttl=60)
def status() -> dict:
    s = store()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return s.status_counts(now - timedelta(days=7))
    finally:
        s.close()
