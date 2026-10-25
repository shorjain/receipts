"""Match an external market to a GAMES row by its two teams and date."""
from __future__ import annotations

from datetime import date, timedelta, timezone


def match_by_teams(pair: tuple[str, str], day: date, games: list[dict]) -> dict | None:
    """Same two teams, kickoff within a day of `day` (an Eastern-time date)."""
    for g in games:
        if {g["home"], g["away"]} == set(pair):
            ko_et = (g["kickoff_at"].replace(tzinfo=timezone.utc) - timedelta(hours=5)).date()
            if abs((ko_et - day).days) <= 1:
                return g
    return None
