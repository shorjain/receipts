from datetime import datetime, timedelta

import pytest

from pipelines import compare_venues as cv
from pipelines.detect_moves import Point

T0 = datetime(2026, 10, 25, 14, 0)
CFG = {"lookback_minutes": 30, "reaction_fraction": 0.5, "reaction_after_minutes": 30}


def pts(values):
    """values: list of (minute, prob)."""
    return [Point(T0 + timedelta(minutes=m), p, None) for m, p in values]


def test_align_as_of_with_tolerance():
    a = pts([(0, .40), (5, .42)])
    b = pts([(1, .41), (30, .45)])
    out = cv.align(a, b, "prob", 11)
    assert [(int((t - T0).total_seconds() // 60), va, vb) for t, va, vb in out] == [(1, .40, .41), (5, .42, .41)]
    # minute 30: a's last value is 25 minutes old, past tolerance, so it's dropped


def test_gap_where_follower_catches_up():
    # Polymarket jumps first at minute 2; Kalshi follows at minute 6.
    a = pts([(0, .40), (2, .40), (4, .40), (6, .45)])
    b = pts([(0, .40), (2, .45), (4, .45), (6, .45)])
    eps = cv.gap_episodes(cv.align(a, b, "prob", 11), 0.025, 2)
    assert len(eps) == 1
    e = eps[0]
    assert e["mover"] == "polymarket" and e["resolution"] == "follower_caught_up"
    assert e["peak_gap"] == pytest.approx(0.05)
    assert e["gap_start"] == T0 + timedelta(minutes=2) and e["gap_end"] == T0 + timedelta(minutes=4)


def test_gap_where_mover_reverts():
    a = pts([(0, .40), (2, .44), (4, .44), (6, .40)])
    b = pts([(0, .40), (2, .40), (4, .40), (6, .40)])
    e = cv.gap_episodes(cv.align(a, b, "prob", 11), 0.025, 2)[0]
    assert e["mover"] == "kalshi" and e["resolution"] == "mover_reverted"


def test_short_or_small_gaps_are_ignored():
    a = pts([(0, .40), (2, .44), (4, .40)])
    b = pts([(0, .40), (2, .40), (4, .40)])
    assert cv.gap_episodes(cv.align(a, b, "prob", 11), 0.025, 2) == []      # one point only
    c = pts([(0, .40), (2, .42), (4, .42)])
    assert cv.gap_episodes(cv.align(c, b, "prob", 11), 0.025, 2) == []      # 2 pp < 2.5 pp


def test_standing_offset_is_not_a_gap():
    # Polymarket sits 3 pp above Kalshi for hours: that's the baseline, not news.
    a = pts([(m, .40) for m in range(0, 240, 5)])
    b = pts([(m, .43) for m in range(0, 240, 5)])
    eps = cv.gap_episodes(cv.align(a, b, "prob", 11), 0.025, 2, baseline_minutes=180)
    assert [e["gap_start"] for e in eps] == [T0]          # only before any history exists
    b2 = pts([(m, .43 if m < 200 else .46) for m in range(0, 240, 5)])
    eps2 = cv.gap_episodes(cv.align(a, b2, "prob", 11), 0.025, 2, baseline_minutes=180)
    assert len(eps2) == 2 and eps2[1]["baseline_gap"] == pytest.approx(0.03)
    assert eps2[1]["peak_gap"] == pytest.approx(0.06)


def test_open_gap_at_end_of_data():
    a = pts([(0, .40), (2, .40), (4, .40)])
    b = pts([(0, .40), (2, .46), (4, .47)])
    assert cv.gap_episodes(cv.align(a, b, "prob", 11), 0.025, 2)[0]["resolution"] == "open"


def test_reaction_times_and_leader():
    move = {"market_type": "winner", "prob_before": .40, "prob_after": .46,
            "window_start": T0 + timedelta(minutes=10), "window_end": T0 + timedelta(minutes=25)}
    by_venue = {
        "kalshi": pts([(-30, .40), (12, .41), (18, .44), (25, .46)]),     # half move (.43) at 18
        "polymarket": pts([(-30, .40), (11, .44), (20, .46)]),            # at 11
        "pinnacle": pts([(-30, .40), (40, .41)]),                         # never reacts
    }
    r = cv.reaction_times(move, by_venue, CFG)
    assert r == {"kalshi": 8.0, "polymarket": 1.0, "pinnacle": None}      # halfway price is .43
    assert cv.leader(r) == ("polymarket", 7.0)


def test_reaction_needs_to_start_on_the_pre_move_side():
    # A reversal: the venue sat at .815 (previous level) before dipping to .785 and
    # coming back. It must count when it crosses .80 on the way back up, not at once.
    move = {"market_type": "winner", "prob_before": .785, "prob_after": .815,
            "window_start": T0 + timedelta(minutes=30), "window_end": T0 + timedelta(minutes=50)}
    by_venue = {"kalshi": pts([(0, .815), (10, .785), (30, .785), (45, .805), (50, .815)])}
    assert cv.reaction_times(move, by_venue, CFG) == {"kalshi": 15.0}


def test_reaction_before_window_is_negative():
    move = {"market_type": "spread", "line_before": -2.5, "line_after": -3.5,
            "window_start": T0 + timedelta(minutes=10), "window_end": T0 + timedelta(minutes=20)}
    by_venue = {"polymarket": [Point(T0 + timedelta(minutes=m), None, v) for m, v in [(-25, -2.5), (5, -3.1)]]}
    assert cv.reaction_times(move, by_venue, CFG) == {"polymarket": -5.0}


def test_summarize_counts():
    moves = [{"venue_reactions": {"kalshi": 8, "polymarket": 1}},
             {"venue_reactions": {"kalshi": 0, "polymarket": 3}},
             {"venue_reactions": {"kalshi": 2, "polymarket": 2.5}},
             {"venue_reactions": {"kalshi": 2}}]
    gaps = [{"mover": "polymarket", "resolution": "follower_caught_up"},
            {"mover": "kalshi", "resolution": "mover_reverted"}]
    s = cv.summarize(moves, gaps)
    assert s["moves_both_reacted"] == 3 and s["polymarket_first"] == 1 and s["kalshi_first"] == 1
    assert s["same_minute"] == 1 and s["median_polymarket_lead_minutes"] == -0.5
    assert s["gaps_polymarket_led_and_was_followed"] == 1 and s["gaps_kalshi_led_and_was_followed"] == 0
