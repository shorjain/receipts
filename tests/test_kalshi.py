from datetime import date, datetime

import pytest

from sources import kalshi
from tests.conftest import load_fixture


def raw():
    return {s: load_fixture(f"kalshi_{s.lower()}.json") for s in kalshi.SERIES}


def test_parse_event_ticker():
    assert kalshi.parse_event_ticker("KXNFLGAME-26OCT25LARLV") == (date(2026, 10, 25), ("LA", "LV"))
    assert kalshi.parse_event_ticker("garbage") is None


def test_match_game_needs_same_teams_and_date(la_lv_game):
    assert kalshi.match_game("KXNFLGAME-26OCT25LARLV", [la_lv_game]) is la_lv_game
    assert kalshi.match_game("KXNFLGAME-26NOV25LARLV", [la_lv_game]) is None
    assert kalshi.match_game("KXNFLGAME-26OCT25CHISEA", [la_lv_game]) is None


def test_implied_median_interpolates_and_fixes_inversions():
    assert kalshi.implied_median([(2.5, 0.6), (3.5, 0.4)]) == pytest.approx(3.0)
    # inverted rung at 4.5 is clamped to the running minimum
    assert kalshi.implied_median([(2.5, 0.7), (3.5, 0.45), (4.5, 0.55)]) == pytest.approx(2.5 + 0.2 / 0.25)
    assert kalshi.implied_median([(1.5, 0.9), (2.5, 0.8)]) is None
    assert kalshi.implied_median([(1.5, 0.9)]) is None


def test_liquidity_rules():
    q = {"bid": 0.50, "ask": 0.53, "volume": 1000}
    assert kalshi.is_liquid(q, 0.04, 500)
    assert not kalshi.is_liquid({**q, "ask": 0.54}, 0.04, 500)   # spread 4c is not under 4c
    assert not kalshi.is_liquid({**q, "volume": 499}, 0.04, 500)
    assert not kalshi.is_liquid({**q, "bid": 0.0}, 0.04, 500)


def test_normalize_recorded_fixture(la_lv_game):
    t = datetime(2026, 10, 25, 12, 0)
    markets, snaps = kalshi.normalize(raw(), [la_lv_game], t, max_spread=0.04, min_volume=0)
    derived = {s["market_id"]: s for s in snaps if s["source"] == "kalshi_derived"}
    win = derived["kalshi:2026_07_LA_LV:winner"]
    assert win["implied_prob"] == pytest.approx(0.395 / (0.395 + 0.605))
    # nflverse closed LV +3.0 / 48.5; the Kalshi ladder should land close to that.
    assert derived["kalshi:2026_07_LA_LV:spread"]["line"] == pytest.approx(3.0, abs=0.5)
    assert derived["kalshi:2026_07_LA_LV:total"]["line"] == pytest.approx(48.5, abs=1.0)
    assert derived["kalshi:2026_07_LA_LV:spread"]["rungs"]                       # rung set recorded
    raw_rows = [s for s in snaps if s["source"] == "kalshi_rest"]
    assert len(raw_rows) == sum(len(v) for v in raw().values())
    assert all(m["game_id"] == "2026_07_LA_LV" for m in markets)
    sides = {m["side"] for m in markets if m["market_type"] == "winner" and m["source_ref"] != "derived"}
    assert sides == {"LA", "LV"}


def test_unmatched_games_are_skipped():
    other = {"game_id": "x", "home": "SEA", "away": "CHI", "kickoff_at": datetime(2026, 11, 3, 1, 15)}
    markets, snaps = kalshi.normalize(raw(), [other], datetime(2026, 10, 25), 0.04, 0)
    assert markets == [] and snaps == []
