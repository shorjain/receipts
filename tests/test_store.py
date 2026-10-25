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


def test_upsert_stamps_updated_at(store):
    store.upsert_games([{"game_id": "g1", "season": 2026}])
    first = store.get_games()[0]["updated_at"]
    assert first is not None
    store.upsert_games([{"game_id": "g1", "home_score": 3, "updated_at": datetime(2030, 1, 1)}])
    assert store.get_games()[0]["updated_at"] == datetime(2030, 1, 1)


def test_sync_copies_only_new_rows_and_skips_embeddings(store):
    from pipelines.sync_snowflake import sync
    from store.duckdb import DuckDBStore
    remote = DuckDBStore(":memory:")
    remote.create_schema()
    store.upsert_games([{"game_id": "g1", "season": 2026, "updated_at": T0}])
    store.upsert_line_moves([{"move_id": "m1", "embedding": [0.5] * 768, "summary_text": "x",
                              "updated_at": T0}])
    assert sync(store, remote, now=T0 + timedelta(minutes=1)) == {"GAMES": 1, "LINE_MOVES": 1}
    moved = remote.line_moves()[0]
    assert moved["summary_text"] == "x" and moved["embedding"] is None
    # nothing new -> nothing copied; a later write is picked up on the next run
    assert sync(store, remote, now=T0 + timedelta(minutes=2)) == {}
    store.upsert_games([{"game_id": "g1", "home_score": 7, "updated_at": T0 + timedelta(minutes=3)}])
    assert sync(store, remote, now=T0 + timedelta(minutes=4)) == {"GAMES": 1}
    assert remote.get_games()[0]["home_score"] == 7
    assert remote.sync_state() == {}          # bookkeeping stays local


def test_snapshot_is_a_readable_copy(tmp_path):
    from store.duckdb import DuckDBStore
    src = DuckDBStore(tmp_path / "main.duckdb")
    src.create_schema()
    src.upsert_games([{"game_id": "g1", "season": 2026}])
    src.snapshot(tmp_path / "snap.duckdb")
    src.upsert_games([{"game_id": "g2", "season": 2026}])      # writer keeps going
    ro = DuckDBStore(tmp_path / "snap.duckdb", read_only=True)
    assert [g["game_id"] for g in ro.get_games()] == ["g1"]
    ro.close()
    src.snapshot(tmp_path / "snap.duckdb")
    assert len(DuckDBStore(tmp_path / "snap.duckdb", read_only=True).get_games()) == 2
