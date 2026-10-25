from datetime import datetime, timedelta

from pipelines import detect_moves, ingest_odds
from sources import kalshi
from tests.conftest import load_fixture

T0 = datetime(2026, 10, 25, 14, 0)


def test_detect_run_end_to_end_is_idempotent(store, la_lv_game):
    store.upsert_games([la_lv_game])
    store.upsert_markets([{"market_id": f"{v}:g:winner", "venue": v, "game_id": "2026_07_LA_LV",
                           "market_type": "winner", "side": "home"} for v in ("pinnacle", "consensus")])
    store.insert_odds_snapshots(
        [{"market_id": "pinnacle:g:winner", "taken_at": T0 + timedelta(minutes=m), "implied_prob": p, "source": "t"}
         for m, p in [(0, .40), (10, .45)]] +
        [{"market_id": "consensus:g:winner", "taken_at": T0 + timedelta(minutes=m), "implied_prob": p, "source": "t"}
         for m, p in [(5, .40), (20, .44)]])
    now = T0 + timedelta(hours=1)
    detect_moves.run(store, hours=6, now=now)
    detect_moves.run(store, hours=6, now=now)
    moves = store.line_moves()
    assert len(moves) == 1 and moves[0]["venue_scope"] == "multi" and moves[0]["explain_status"] == "pending"


def test_kalshi_poll_stores_only_changes(store, la_lv_game, monkeypatch):
    monkeypatch.setenv("KALSHI_MIN_VOLUME", "0")
    store.upsert_games([la_lv_game])

    class FakeClient:
        def __init__(self, bump=0.0):
            self.bump = bump

        def list_markets(self, series):
            ms = load_fixture(f"kalshi_{series.lower()}.json")
            if self.bump:
                for m in ms:
                    for k in ("yes_bid_dollars", "yes_ask_dollars"):
                        m[k] = f"{float(m[k]) + self.bump:.4f}"
            return ms

    first = ingest_odds.poll_kalshi(store, FakeClient(), [la_lv_game], T0)
    again = ingest_odds.poll_kalshi(store, FakeClient(), [la_lv_game], T0 + timedelta(minutes=1))
    moved = ingest_odds.poll_kalshi(store, FakeClient(0.02), [la_lv_game], T0 + timedelta(minutes=2))
    beat = ingest_odds.poll_kalshi(store, FakeClient(0.02), [la_lv_game], T0 + timedelta(minutes=13))
    assert first > 0 and again == 0 and moved > 0 and beat == first


def test_should_store_rules():
    last = {"taken_at": T0, "implied_prob": 0.50, "line": None}
    s = {"implied_prob": 0.504, "line": None}
    assert not ingest_odds.should_store(s, last, 0.005, T0 + timedelta(minutes=1))
    assert ingest_odds.should_store({**s, "implied_prob": 0.505}, last, 0.005, T0 + timedelta(minutes=1))
    assert ingest_odds.should_store(s, last, 0.005, T0 + timedelta(minutes=10))
    assert ingest_odds.should_store(s, None, 0.005, T0)


def test_odds_api_schedule(la_lv_game):
    sunday_10am_et = datetime(2026, 10, 25, 14, 0)      # 10:00 EDT, game at 16:25 ET
    assert ingest_odds.odds_api_interval(sunday_10am_et, [la_lv_game]) == 10
    sunday_8am_et = datetime(2026, 10, 25, 12, 0)
    assert ingest_odds.odds_api_interval(sunday_8am_et, [la_lv_game]) == 30
    tuesday = datetime(2026, 10, 20, 16, 0)
    assert ingest_odds.odds_api_interval(tuesday, [la_lv_game]) == 30
    assert ingest_odds.odds_api_interval(datetime(2026, 10, 26), [la_lv_game]) is None  # nothing upcoming
