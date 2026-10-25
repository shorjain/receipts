from datetime import datetime, timedelta

T0 = datetime(2026, 10, 25, 14, 0)


def test_upsert_is_idempotent(store, la_lv_game):
    store.upsert_games([la_lv_game])
    store.upsert_games([{**la_lv_game, "home_score": 24}])
    rows = store.get_games(2026)
    assert len(rows) == 1 and rows[0]["home_score"] == 24


def test_upsert_keeps_unspecified_columns(store, la_lv_game):
    store.upsert_games([{**la_lv_game, "roof": "dome"}])
    store.upsert_games([{"game_id": la_lv_game["game_id"], "home_score": 10}])
    assert store.get_games()[0]["roof"] == "dome"


def test_vector_and_json_round_trip(store):
    store.upsert_line_moves([{"move_id": "m1", "embedding": [0.5] * 768, "window_start": T0}])
    store.upsert("SEARCH_RUNS", [{"run_id": "r1", "raw_response": {"a": [1, 2]}}])
    assert len(store.line_moves()[0]["embedding"]) == 768


def test_odds_series_and_latest(store, la_lv_game):
    store.upsert_games([la_lv_game])
    store.upsert_markets([{"market_id": "pinnacle:g:winner", "venue": "pinnacle", "game_id": "2026_07_LA_LV",
                           "market_type": "winner", "side": "home"}])
    store.insert_odds_snapshots([{"market_id": "pinnacle:g:winner", "taken_at": T0 + timedelta(minutes=m),
                                  "implied_prob": p, "source": "t"} for m, p in [(0, .40), (10, .45)]])
    rows = store.odds_series(T0, T0 + timedelta(hours=1), market_type="winner")
    assert [r["implied_prob"] for r in rows] == [.40, .45] and rows[0]["venue"] == "pinnacle"
    assert store.latest_snapshots()["pinnacle:g:winner"]["implied_prob"] == .45
