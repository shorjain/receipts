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


def test_compare_venues_run_writes_gaps_and_reactions(store, la_lv_game):
    from pipelines import compare_venues
    store.upsert_games([la_lv_game])
    store.upsert_markets([{"market_id": f"{v}:2026_07_LA_LV:winner", "venue": v, "game_id": "2026_07_LA_LV",
                           "market_type": "winner", "side": "home"} for v in ("kalshi", "polymarket")])
    rows = []
    for v, path in [("kalshi", [(0, .40), (2, .40), (4, .40), (6, .45), (8, .45)]),
                    ("polymarket", [(0, .40), (2, .45), (4, .45), (6, .45), (8, .45)])]:
        rows += [{"market_id": f"{v}:2026_07_LA_LV:winner", "taken_at": T0 + timedelta(minutes=m),
                  "implied_prob": p, "bid": p - .005, "ask": p + .005, "volume": 1e5,
                  "source": f"{v}_derived"} for m, p in path]
    store.insert_odds_snapshots(rows)
    now = T0 + timedelta(hours=1)
    detect_moves.run(store, hours=6, now=now)
    for _ in range(2):  # idempotent
        summary = compare_venues.run(store, hours=6, now=now)
    gaps = store.venue_gaps()
    assert len(gaps) == 1 and gaps[0]["mover"] == "polymarket" and gaps[0]["resolution"] == "follower_caught_up"
    move = store.line_moves()[0]
    assert move["leader_venue"] == "polymarket" and move["lead_lag_minutes"] == 4.0
    assert summary["polymarket_first"] == 1


def test_daily_allowance_spreads_credits_over_the_month():
    oct25 = datetime(2026, 10, 25, 14)                  # 7 days left including today
    assert ingest_odds.daily_allowance(491, oct25, 3, 50) == 21
    assert ingest_odds.daily_allowance(500, datetime(2026, 11, 1, 14), 3, 50) == 5
    assert ingest_odds.daily_allowance(60, oct25, 3, 50) == 0
    assert ingest_odds.daily_allowance(None, oct25, 3, 50) == 1


def test_paced_schedule_baselines_moves_and_allowance(store, la_lv_game, monkeypatch):
    monkeypatch.setenv("ODDS_API_MIN_CREDITS", "50")
    store.upsert_games([la_lv_game])
    nine_et, ten_oh5_et = datetime(2026, 10, 25, 13, 0), datetime(2026, 10, 25, 14, 5)
    assert ingest_odds.odds_api_reason(store, nine_et, [la_lv_game], 3, 400) is None
    assert ingest_odds.odds_api_reason(store, ten_oh5_et, [la_lv_game], 3, 400) == "baseline 10:00 ET"
    store.upsert("API_USAGE", [{"called_at": ten_oh5_et, "source": "odds_api", "endpoint": "/odds",
                                "cost_units": 3, "remaining": 397}])
    later = ten_oh5_et + timedelta(minutes=45)
    assert ingest_odds.odds_api_reason(store, later, [la_lv_game], 3, 397) is None
    store.upsert_line_moves([{"move_id": "m", "game_id": "2026_07_LA_LV", "market_type": "winner",
                              "venues": "kalshi", "window_start": later - timedelta(minutes=20),
                              "window_end": later - timedelta(minutes=5)}])
    assert ingest_odds.odds_api_reason(store, later, [la_lv_game], 3, 397) == "move in 2026_07_LA_LV"
    # cooldown: a move 10 minutes after the last poll waits
    assert ingest_odds.odds_api_reason(store, ten_oh5_et + timedelta(minutes=10), [la_lv_game], 3, 397) is None
    # out of allowance for today
    assert ingest_odds.odds_api_reason(store, later, [la_lv_game], 3, 52) is None
