from datetime import datetime, timedelta

import pytest

from pipelines import backfill as bf

KO = datetime(2026, 10, 18, 17, 0)
GAME = {"game_id": "2026_07_DAL_GB", "season": 2026, "week": 7, "game_type": "REG", "home": "GB", "away": "DAL",
        "kickoff_at": KO}


def candle(ts: datetime, bid: float, ask: float, vol: float = 10.0) -> dict:
    return {"end_period_ts": int((ts - datetime(1970, 1, 1)).total_seconds()),
            "yes_bid": {"close_dollars": f"{bid:.2f}"}, "yes_ask": {"close_dollars": f"{ask:.2f}"}, "volume_fp": str(vol)}


def test_kalshi_series_normalizes_and_tracks_24h_volume():
    t0 = KO - timedelta(days=2)
    home = [candle(t0, .59, .61, 100), candle(t0 + timedelta(hours=25), .64, .66, 5)]
    away = [candle(t0, .39, .41)]
    rows = bf.kalshi_series(home, away)
    assert rows[0]["p"] == pytest.approx(0.60) and rows[0]["volume"] == 100
    assert rows[1]["p"] == pytest.approx(0.65 / (0.65 + 0.40))    # away carried forward
    assert rows[1]["volume"] == 5                                   # day-old volume rolled off


def test_compress_keeps_changes_and_heartbeats():
    t0 = KO - timedelta(hours=1)
    pts = [(t0 + timedelta(minutes=m), v) for m, v in [(0, .50), (1, .502), (2, .506), (3, .507), (14, .507)]]
    assert [m for m, _ in bf.compress(pts, 0.005)] == [pts[0][0], pts[2][0], pts[4][0]]


class FakeKalshi:
    def __init__(self):
        self.calls = 0

    def list_markets(self, series, status=None):
        return [{"event_ticker": "KXNFLGAME-26OCT18DALGB", "ticker": f"KXNFLGAME-26OCT18DALGB-{t}",
                 "open_time": "2026-10-10T00:00:00Z"} for t in ("GB", "DAL")]

    def candlesticks(self, series, ticker, start, end, period):
        self.calls += 1
        if start > KO - timedelta(hours=3):
            return []
        p = .60 if ticker.endswith("GB") else .40
        out = []
        for m in range(0, 60):            # 1 h of candles; GB jumps +5 pp at minute 30
            jump = .05 if ticker.endswith("GB") and m >= 30 else (-.05 if m >= 30 else 0)
            out.append(candle(KO - timedelta(hours=4) + timedelta(minutes=m), p + jump - .01, p + jump + .01, 1e5))
        return out


class FakePoly:
    def gamma_markets(self, smt, end_min, end_max, closed=False):
        return [{"id": "9", "sportsMarketType": "moneyline", "events": [{"ticker": "nfl-dal-gb-2026-10-18"}],
                 "outcomes": '["Cowboys", "Packers"]', "clobTokenIds": '["tok", "tok2"]', "question": "Cowboys vs. Packers"}]

    def prices_history(self, token, start, end, fidelity):
        t0 = KO - timedelta(hours=4)
        return [{"t": (t0 + timedelta(minutes=m) - datetime(1970, 1, 1)).total_seconds(), "p": .40 - (.05 if m >= 25 else 0)}
                for m in range(60)]


def test_backfill_end_to_end_is_resumable(store, monkeypatch):
    store.upsert_games([GAME])
    k = FakeKalshi()
    monkeypatch.setattr(bf, "_clients", {"kalshi": k, "poly": FakePoly(), "x": None})
    now = KO + timedelta(days=7)
    bf.step(store, seconds=0, now=now, season=2026)
    prog = bf.progress(store)
    assert prog["kalshi"] == {"done": 1} and prog["poly"] == {"done": 1} and prog["detect"] == {"done": 1}
    moves = store.line_moves(game_id=GAME["game_id"])
    assert len(moves) == 1 and moves[0]["venue_scope"] == "multi"           # Kalshi + Polymarket agree
    assert moves[0]["explain_status"] == "searched"                         # linked (stored posts; X off)
    assert prog["search"] == {"done": 1}
    calls = k.calls
    bf.step(store, seconds=0, now=now + timedelta(minutes=5), season=2026)  # nothing redone
    assert k.calls == calls and len(store.line_moves()) == 1


def test_backfill_skips_unfinished_games(store, monkeypatch):
    store.upsert_games([{**GAME, "kickoff_at": datetime(2026, 10, 25, 17)}])
    monkeypatch.setattr(bf, "_clients", {"kalshi": FakeKalshi(), "poly": FakePoly(), "x": None})
    assert bf.plan(store, FakeKalshi(), FakePoly(), datetime(2026, 10, 25, 18), 2026) == 0


def test_backfill_xai_cap(store, monkeypatch):
    monkeypatch.setenv("BACKFILL_XAI_USD", "1.0")
    store.insert_search_run({"run_id": "r", "purpose": "backfill", "cost_estimate_usd": 1.2, "ran_at": KO})
    assert bf.backfill_xai_left(store, datetime(2026, 8, 1)) == pytest.approx(-0.2)


def test_migration_drops_polymarket_only_moves_and_redoes_detection(store):
    store.upsert_games([GAME])
    store.upsert_markets([{"market_id": "polymarket:2026_07_DAL_GB:winner", "venue": "polymarket",
                           "game_id": GAME["game_id"], "market_type": "winner", "side": "home"}])
    store.insert_odds_snapshots([{"market_id": "polymarket:2026_07_DAL_GB:winner", "taken_at": KO - timedelta(hours=5),
                                  "implied_prob": .4, "source": "polymarket_derived"}])
    store.upsert_line_moves([
        {"move_id": "poly", "game_id": GAME["game_id"], "venues": "polymarket", "window_start": KO - timedelta(days=2),
         "window_end": KO - timedelta(days=2) + timedelta(minutes=30), "explain_status": "pending"},
        {"move_id": "both", "game_id": GAME["game_id"], "venues": "kalshi,polymarket",
         "window_start": KO - timedelta(days=1), "window_end": KO - timedelta(days=1) + timedelta(minutes=30),
         "explain_status": "pending"}])
    store.upsert_backfill_tasks([
        {"task_id": "detect:2026_07_DAL_GB", "kind": "detect", "game_id": GAME["game_id"], "status": "done", "priority": 3},
        {"task_id": "search:poly", "kind": "search", "game_id": GAME["game_id"], "status": "pending", "priority": 1}])
    bf.migrate_v2(store, {GAME["game_id"]: GAME})
    assert [m["move_id"] for m in store.line_moves()] == ["both"]
    status = {t["task_id"]: t["status"] for t in store.backfill_tasks()}
    assert status["detect:2026_07_DAL_GB"] == "pending" and status["search:poly"] == "obsolete"
    assert store.odds_series(KO - timedelta(days=1), KO)[0]["source"] == "polymarket_history"
    bf.migrate_v2(store, {GAME["game_id"]: GAME})          # runs once only
    assert {t["task_id"]: t["status"] for t in store.backfill_tasks()}["detect:2026_07_DAL_GB"] == "pending"


def test_rung_selection_near_the_close():
    g = {**GAME, "close_spread_line": -3.0, "close_total_line": 45.5}          # GB favored by 3
    assert bf.rung_near_close("kalshi_spread", g, "GB", 2.5)                  # home by > 2.5
    assert bf.rung_near_close("kalshi_spread", g, "DAL", 0.5)                 # away by > 0.5 -> margin < -0.5
    assert not bf.rung_near_close("kalshi_spread", g, "GB", 13.5)
    assert not bf.rung_near_close("kalshi_spread", g, "DAL", 7.5)             # margin -7.5 is 10.5 off
    assert bf.rung_near_close("kalshi_total", g, "over", 48.5) and not bf.rung_near_close("kalshi_total", g, "over", 52.5)
    assert bf.rung_near_close("kalshi_total", {**g, "close_total_line": None}, "over", 70.5)


def test_ladder_series_rebuilds_the_spread_line_and_rungs():
    t0 = KO - timedelta(hours=3)
    rungs = [{"ticker": "GB2", "side": "GB", "strike": 2.5}, {"ticker": "GB4", "side": "GB", "strike": 4.5}]
    def c(m, bid):
        return candle(t0 + timedelta(minutes=m), bid, bid + .01, 1000)
    candles = {"GB2": [c(0, .60), c(30, .65)], "GB4": [c(0, .40), c(30, .45)]}
    rows = bf.ladder_series(candles, rungs, GAME, "spread", 0.04, 500)
    assert [r["t"] for r in rows] == [t0, t0 + timedelta(minutes=30)]
    assert rows[0]["line"] == pytest.approx(-3.55, abs=0.01)                   # median margin ~3.55
    assert rows[1]["line"] == pytest.approx(-4.05, abs=0.01)                   # market moved half a point
    assert rows[0]["rungs"] == "2.5,4.5"
    kept = bf.thin_lines(rows + [{**rows[1], "t": rows[1]["t"] + timedelta(minutes=1), "line": rows[1]["line"] + 0.05}])
    assert len(kept) == 2                                                      # tiny wiggle dropped


def test_finished_ladder_reopens_detection(store):
    store.upsert_games([{**GAME, "close_spread_line": -3.0}])
    store.upsert_backfill_tasks([{"task_id": "detect:2026_07_DAL_GB", "kind": "detect", "game_id": GAME["game_id"],
                                  "status": "done", "priority": 3}])

    class K:
        def candlesticks(self, series, ticker, s, e, p):
            return []
    task = {"task_id": "kalshi_spread:2026_07_DAL_GB", "kind": "kalshi_spread", "game_id": GAME["game_id"],
            "detail": {"rungs": [{"ticker": "x", "side": "GB", "strike": 2.5}], "open": "2026-10-10T00:00:00Z"}}
    bf.fetch_ladder(store, K(), task, {**GAME, "close_spread_line": -3.0})
    assert store.backfill_tasks(kind="detect")[0]["status"] == "pending"
