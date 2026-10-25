from datetime import datetime

import pandas as pd

from sources import nflverse
from tests.conftest import load_fixture


def test_kickoff_converts_eastern_to_utc():
    assert nflverse.kickoff_utc("2026-10-25", "16:25") == datetime(2026, 10, 25, 20, 25)   # EDT
    assert nflverse.kickoff_utc("2026-12-06", "13:00") == datetime(2026, 12, 6, 18, 0)     # EST


def test_schedules_to_games_fixture():
    rows = nflverse.schedules_to_games(pd.DataFrame(load_fixture("nflverse_schedules.json")))
    g = rows[0]
    assert g["game_id"] == "2026_01_NE_SEA" and g["home"] == "SEA" and g["away"] == "NE"
    assert g["kickoff_at"] == datetime(2026, 9, 10, 0, 20)
    assert g["close_spread_line"] == -3.0          # SEA favored by 3 -> home -3
    assert g["home_score"] == 13 and g["div_game"] is False


def test_rosters_to_rows_fixture():
    rows = nflverse.rosters_to_rows(pd.DataFrame(load_fixture("nflverse_rosters_weekly.json")))
    assert rows and all(r["player_id"] and r["team"] in ("SEA", "NE") for r in rows)
