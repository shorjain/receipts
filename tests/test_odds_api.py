from datetime import datetime

import pytest

from sources import odds_api
from tests.conftest import load_fixture


def test_american_to_prob():
    assert odds_api.american_to_prob(100) == pytest.approx(0.5)
    assert odds_api.american_to_prob(-150) == pytest.approx(0.6)
    assert odds_api.american_to_prob(150) == pytest.approx(0.4)


def test_no_vig_sums_to_one():
    a, b = odds_api.no_vig(odds_api.american_to_prob(-110), odds_api.american_to_prob(-110))
    assert a == pytest.approx(0.5) and a + b == pytest.approx(1.0)


def test_normalize_sample(la_lv_game):
    events = load_fixture("odds_api_odds_sample.json")
    markets, snaps = odds_api.normalize(events, [la_lv_game], datetime(2026, 10, 25, 16))
    by_id = {s["market_id"]: s for s in snaps}
    pin = by_id["pinnacle:2026_07_LA_LV:winner"]
    p_home = odds_api.american_to_prob(150)
    assert pin["implied_prob"] == pytest.approx(p_home / (p_home + odds_api.american_to_prob(-170)))
    assert by_id["pinnacle:2026_07_LA_LV:spread"]["line"] == 3.0
    assert by_id["consensus:2026_07_LA_LV:spread"]["line"] == 3.0          # median of 3.0, 3.5, 3.0
    assert by_id["consensus:2026_07_LA_LV:total"]["line"] == pytest.approx(48.25)
    assert by_id["consensus:2026_07_LA_LV:winner"]["volume"] == 3          # book count
    assert all("Nowhere" not in m["source_ref"] for m in markets)


def test_cost_estimate_counts_book_groups():
    c = odds_api.OddsApiClient(api_key="test")
    c.bookmakers = ["b%d" % i for i in range(6)]
    assert c.estimated_cost(["h2h", "spreads", "totals"]) == 3
    c.bookmakers = ["b%d" % i for i in range(11)]
    assert c.estimated_cost(["h2h", "spreads", "totals"]) == 6
    assert c.estimated_cost(["h2h"], historical=True) == 20


def test_credit_floor_blocks_calls(monkeypatch):
    monkeypatch.setenv("ODDS_API_MIN_CREDITS", "50")
    c = odds_api.OddsApiClient(api_key="test")
    c.remaining = 51
    with pytest.raises(odds_api.CreditsExhausted):
        c.odds()
