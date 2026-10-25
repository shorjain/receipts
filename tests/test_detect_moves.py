from datetime import datetime, timedelta

import pytest

from pipelines import detect_moves as dm

T0 = datetime(2026, 10, 25, 14, 0)
LIQ = {"kalshi": (0.04, 500), "polymarket": (0.04, 500)}


def series(values, minutes, key="prob"):
    return [dm.Point(T0 + timedelta(minutes=m), v if key == "prob" else None, v if key == "line" else None)
            for v, m in zip(values, minutes)]


def snap(venue, mtype, minute, prob=None, line=None, source=None, bid=None, ask=None, volume=None,
         game_id="2026_07_LA_LV"):
    return {"venue": venue, "game_id": game_id, "market_type": mtype, "taken_at": T0 + timedelta(minutes=minute),
            "implied_prob": prob, "line": line, "bid": bid, "ask": ask, "volume": volume,
            "source": source or f"{venue}_test"}


# ---- thresholds ---------------------------------------------------------------
def test_prob_just_under_threshold_is_not_a_move(cfg):
    assert dm.find_candidates("g", "winner", "pinnacle", series([0.500, 0.529], [0, 10]), cfg) == []


def test_prob_at_threshold_is_a_move(cfg):
    c = dm.find_candidates("g", "winner", "pinnacle", series([0.50, 0.53], [0, 10]), cfg)
    assert len(c) == 1 and c[0].magnitude == pytest.approx(1.0)


def test_prob_change_outside_lookback_is_not_a_move(cfg):
    assert dm.find_candidates("g", "winner", "pinnacle", series([0.50, 0.56], [0, 31]), cfg) == []
    assert len(dm.find_candidates("g", "winner", "pinnacle", series([0.50, 0.56], [0, 30]), cfg)) == 1


@pytest.mark.parametrize("a,b,expected", [
    (-2.5, -3.5, [3]), (-3.5, -2.5, [3]), (-3.0, -3.5, [3]), (-6.5, -7.5, [7]),
    (-2.5, -7.5, [3, 7]), (-3.5, -4.5, []), (2.5, 3.5, [3]), (-1.0, 1.0, []), (-3.0, -3.0, []),
])
def test_crossed_keys(a, b, expected):
    assert dm.crossed_keys(a, b, [3, 7, 10, 14]) == expected


def test_spread_key_cross_is_a_move(cfg):
    c = dm.find_candidates("g", "spread", "pinnacle", series([-2.5, -3.5], [0, 15], "line"), cfg)
    assert len(c) == 1 and c[0].crossed_key == 3 and c[0].magnitude == pytest.approx(1.0)


def test_spread_move_without_key_is_not_a_move(cfg):
    assert dm.find_candidates("g", "spread", "pinnacle", series([-3.5, -5.5], [0, 15], "line"), cfg) == []


def test_tiny_drift_onto_key_is_ignored(cfg):
    # Kalshi-derived lines are continuous; -2.9 -> -3.1 touches 3 but is noise.
    assert dm.find_candidates("g", "spread", "kalshi", series([-2.9, -3.1], [0, 15], "line"), cfg) == []


def test_total_thresholds(cfg):
    assert dm.find_candidates("g", "total", "pinnacle", series([48.5, 47.1], [0, 20], "line"), cfg) == []
    c = dm.find_candidates("g", "total", "pinnacle", series([48.5, 47.0], [0, 20], "line"), cfg)
    assert len(c) == 1 and c[0].magnitude == pytest.approx(1.0)


def test_magnitude_scales_with_size(cfg):
    c = dm.find_candidates("g", "winner", "pinnacle", series([0.50, 0.59], [0, 10]), cfg)
    assert c[0].magnitude == pytest.approx(3.0)


# ---- merging, liquidity, venue scope -----------------------------------------
def test_overlapping_windows_merge_and_multi_venue(cfg, la_lv_game):
    snaps = [
        snap("pinnacle", "winner", 0, prob=0.40), snap("pinnacle", "winner", 10, prob=0.45),
        snap("consensus", "winner", 5, prob=0.40), snap("consensus", "winner", 20, prob=0.44),
    ]
    rows = dm.detect(snaps, {la_lv_game["game_id"]: la_lv_game}, cfg, LIQ)
    assert len(rows) == 1
    r = rows[0]
    assert r["venue_scope"] == "multi" and r["venues"] == "consensus,pinnacle"
    assert r["window_start"] == T0 and r["window_end"] == T0 + timedelta(minutes=20)
    assert r["prob_before"] == pytest.approx(0.40) and r["prob_after"] == pytest.approx(0.45)
    assert "Las Vegas Raiders win probability 40.0% -> 45.0%" in r["summary_text"]


def test_spike_and_reversal_are_two_single_moves(cfg, la_lv_game):
    snaps = [
        snap("pinnacle", "winner", 0, prob=0.40), snap("pinnacle", "winner", 10, prob=0.45),
        snap("consensus", "winner", 5, prob=0.45), snap("consensus", "winner", 15, prob=0.41),
    ]
    rows = dm.detect(snaps, {la_lv_game["game_id"]: la_lv_game}, cfg, LIQ)
    assert len(rows) == 2 and {r["venue_scope"] for r in rows} == {"single"}
    assert all(abs(r["prob_after"] - r["prob_before"]) >= 0.03 for r in rows)


def test_separate_windows_stay_separate(cfg, la_lv_game):
    snaps = [snap("pinnacle", "winner", m, prob=p) for m, p in [(0, .40), (10, .44), (90, .44), (100, .50)]]
    rows = dm.detect(snaps, {la_lv_game["game_id"]: la_lv_game}, cfg, LIQ)
    assert len(rows) == 2


def test_illiquid_kalshi_points_are_ignored(cfg, la_lv_game):
    games = {la_lv_game["game_id"]: la_lv_game}
    wide = [snap("kalshi", "winner", 0, prob=.40, source="kalshi_derived", bid=.38, ask=.42, volume=1000),
            snap("kalshi", "winner", 10, prob=.46, source="kalshi_derived", bid=.44, ask=.48, volume=1000)]
    assert dm.detect(wide, games, cfg, LIQ) == []
    thin = [{**s, "bid": s["implied_prob"] - .01, "ask": s["implied_prob"] + .01, "volume": 10} for s in wide]
    assert dm.detect(thin, games, cfg, LIQ) == []
    ok = [{**s, "volume": 1000} for s in thin]
    assert len(dm.detect(ok, games, cfg, LIQ)) == 1


def test_polymarket_derived_counts_and_merges_with_kalshi(cfg, la_lv_game):
    games = {la_lv_game["game_id"]: la_lv_game}
    q = dict(volume=1e4)
    snaps = [snap("kalshi", "winner", 0, prob=.40, source="kalshi_derived", bid=.39, ask=.41, **q),
             snap("kalshi", "winner", 12, prob=.46, source="kalshi_derived", bid=.45, ask=.47, **q),
             snap("polymarket", "winner", 0, prob=.40, source="polymarket_derived", bid=.39, ask=.41, **q),
             snap("polymarket", "winner", 8, prob=.45, source="polymarket_derived", bid=.44, ask=.46, **q),
             snap("polymarket", "winner", 9, prob=.30, source="polymarket_clob", bid=.29, ask=.31, **q)]
    rows = dm.detect(snaps, games, cfg, LIQ)
    assert len(rows) == 1 and rows[0]["venues"] == "kalshi,polymarket" and rows[0]["venue_scope"] == "multi"


def test_raw_kalshi_tickers_and_other_books_are_not_detected(cfg, la_lv_game):
    games = {la_lv_game["game_id"]: la_lv_game}
    snaps = [snap("kalshi", "winner", 0, prob=.4, source="kalshi_rest", bid=.39, ask=.41, volume=1e4),
             snap("kalshi", "winner", 10, prob=.5, source="kalshi_rest", bid=.49, ask=.51, volume=1e4),
             snap("draftkings", "winner", 0, prob=.4), snap("draftkings", "winner", 10, prob=.5)]
    assert dm.detect(snaps, games, cfg, LIQ) == []


def test_in_game_snapshots_are_ignored(cfg, la_lv_game):
    games = {la_lv_game["game_id"]: la_lv_game}
    after = (la_lv_game["kickoff_at"] - T0).total_seconds() / 60
    snaps = [snap("pinnacle", "winner", after + 1, prob=.4), snap("pinnacle", "winner", after + 5, prob=.7)]
    assert dm.detect(snaps, games, cfg, LIQ) == []


def test_assign_ids_reuses_overlapping_existing_move():
    row = {"game_id": "g", "market_type": "winner", "window_start": T0, "window_end": T0 + timedelta(minutes=20),
           "venue_scope": "single", "venues": "pinnacle", "prob_before": .4, "prob_after": .45,
           "line_before": None, "line_after": None, "crossed_key_number": None}
    existing = [{"move_id": "abc", "game_id": "g", "market_type": "winner", "explain_status": "explained",
                 "window_start": T0 + timedelta(minutes=5), "window_end": T0 + timedelta(minutes=10)}]
    out = dm.assign_ids([row], existing)[0]
    assert out["move_id"] == "abc" and out["explain_status"] == "explained" and out["window_start"] == T0
    fresh = dm.assign_ids([row], [])[0]
    assert fresh["move_id"] == dm.move_id("g", "winner", T0) and fresh["explain_status"] == "pending"


def test_polymarket_history_confirms_but_cannot_create_moves(cfg, la_lv_game):
    games = {la_lv_game["game_id"]: la_lv_game}
    hist = [snap("polymarket", "winner", 0, prob=.40, source="polymarket_history", bid=.395, ask=.405, volume=1e6),
            snap("polymarket", "winner", 10, prob=.46, source="polymarket_history", bid=.455, ask=.465, volume=1e6)]
    assert dm.detect(hist, games, cfg, LIQ) == []
    kal = [snap("kalshi", "winner", 2, prob=.40, source="kalshi_derived", bid=.39, ask=.41, volume=1e4),
           snap("kalshi", "winner", 12, prob=.45, source="kalshi_derived", bid=.44, ask=.46, volume=1e4)]
    rows = dm.detect(hist + kal, games, cfg, LIQ)
    assert len(rows) == 1 and rows[0]["venues"] == "kalshi,polymarket"


def test_ladder_lines_with_different_rungs_are_not_compared(cfg):
    a = dm.Point(T0, None, -2.5, rungs="1.5,2.5,3.5,4.5")
    b = dm.Point(T0 + timedelta(minutes=10), None, -3.5, rungs="2.5,3.5,4.5,5.5")      # overlap 3/5
    c = dm.Point(T0 + timedelta(minutes=10), None, -3.5, rungs="7.5,8.5,9.5")          # overlap 0
    assert len(dm.find_candidates("g", "spread", "kalshi", [a, b], cfg)) == 1
    assert dm.find_candidates("g", "spread", "kalshi", [a, c], cfg) == []
