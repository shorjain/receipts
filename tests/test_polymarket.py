from datetime import date, datetime

import pytest

from sources import polymarket as pm
from tests.conftest import load_fixture


def fixture_catalog(game):
    return [e for m in load_fixture("polymarket_gamma_markets.json") if (e := pm.catalog_entry(m, [game]))]


def test_parse_event_ticker():
    assert pm.parse_event_ticker("nfl-la-lv-2026-10-25") == (date(2026, 10, 25), ("LA", "LV"))
    assert pm.parse_event_ticker("nfl-lar-lv-2026-10-25") == (date(2026, 10, 25), ("LA", "LV"))
    assert pm.parse_event_ticker("pro-football-cardinals-vspt-rams-season-series-winner") is None


def test_catalog_semantics(la_lv_game):
    cat = fixture_catalog(la_lv_game)
    kinds = {e["market_type"] for e in cat}
    assert kinds == {"winner", "spread", "total"}
    assert sum(e["market_type"] == "winner" for e in cat) == 1
    for e in cat:
        if e["market_type"] == "spread":
            assert e["side"] in ("LA", "LV") and e["strike"] > 0     # "Team (-2.5)" -> margin > 2.5
        if e["market_type"] == "total":
            assert e["side"] == "over" and e["strike"] > 20


def test_book_quote_uses_best_levels():
    book = {"bids": [{"price": "0.50", "size": "1"}, {"price": "0.52", "size": "1"}],
            "asks": [{"price": "0.56", "size": "1"}, {"price": "0.54", "size": "1"}], "last_trade_price": "0.53"}
    q = pm.book_quote(book, 100.0)
    assert (q["bid"], q["ask"], q["mid"], q["price"]) == (0.52, 0.54, pytest.approx(0.53), 0.53)
    empty = pm.book_quote({"bids": [], "asks": [], "last_trade_price": "0.4"}, 0)
    assert empty["bid"] is None and empty["mid"] == 0.4


def test_normalize_recorded_fixture(la_lv_game):
    cat = fixture_catalog(la_lv_game)
    books = load_fixture("polymarket_books.json")
    markets, snaps = pm.normalize(cat, books, [la_lv_game], datetime(2026, 10, 25, 14), 0.04, 0)
    d = {s["market_id"]: s for s in snaps if s["source"] == "polymarket_derived"}
    # nflverse closed LV +3.0 / 48.5 and Kalshi had LV ~38-40%; Polymarket should agree.
    assert 0.30 < d["polymarket:2026_07_LA_LV:winner"]["implied_prob"] < 0.48
    assert d["polymarket:2026_07_LA_LV:spread"]["line"] == pytest.approx(3.0, abs=1.0)
    assert d["polymarket:2026_07_LA_LV:total"]["line"] == pytest.approx(48.5, abs=1.5)
    assert sum(s["source"] == "polymarket_clob" for s in snaps) == len(cat)
    assert all(m["venue"] == "polymarket" for m in markets)


def test_winner_complement_gives_home_probability():
    game = {"game_id": "g", "home": "LV", "away": "LA", "kickoff_at": datetime(2026, 10, 25, 20, 25)}
    cat = [{"token_id": "t", "gamma_id": "1", "game_id": "g", "market_type": "winner", "side": "LA",
            "strike": None, "volume": 1e5, "question": "Rams vs. Raiders"}]
    books = [{"asset_id": "t", "bids": [{"price": "0.61", "size": "1"}], "asks": [{"price": "0.62", "size": "1"}]}]
    _, snaps = pm.normalize(cat, books, [game], datetime(2026, 10, 25), 0.04, 0)
    derived = next(s for s in snaps if s["source"] == "polymarket_derived")
    assert derived["implied_prob"] == pytest.approx(1 - 0.615)
    assert (derived["bid"], derived["ask"]) == (pytest.approx(0.38), pytest.approx(0.39))
