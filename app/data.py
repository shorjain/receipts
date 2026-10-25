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
