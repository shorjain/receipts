from datetime import datetime, timedelta

import pytest

from common import insiders
from pipelines import search_moves, search_x, sweep_insiders
from sources import xsearch

T0 = datetime(2026, 10, 25, 14, 0)
ROSTER = [
    {"handle": "AdamSchefter", "tier": "national", "teams": [], "verified": True, "active": True},
    {"handle": "RapSheet", "tier": "national", "teams": [], "verified": True, "active": True},
    {"handle": "VBonsignore", "tier": "beat", "teams": ["LV"], "verified": True, "active": True},
    {"handle": "JourdanRodrigue", "tier": "beat", "teams": ["LA"], "verified": False, "active": True},
    {"handle": "MikeReiss", "tier": "beat", "teams": ["NE"], "verified": True, "active": True},
]


def pid_at(t: datetime, n: int = 0) -> str:
    """A post ID whose embedded timestamp is t."""
    ms = int((t - datetime(1970, 1, 1)).total_seconds() * 1000) - xsearch.TWITTER_EPOCH_MS
    return str((ms << 22) + n)


class FakeClient:
    def __init__(self, posts_by_handle: dict[str, list[str]], cost: float = 0.1):
        self.posts, self.cost, self.calls = posts_by_handle, cost, []

    def search(self, handles, start, end, focus=None):
        self.calls.append((tuple(handles), start, end, focus))
        cits = [f"https://x.com/i/status/{pid}" for h in handles for pid in self.posts.get(h, [])]
        return xsearch.SearchResult(content="model text is ignored", citations=cits,
                                    usage={"prompt_tokens": 1000, "completion_tokens": 100},
                                    tool_calls=len(handles), cost_estimate_usd=self.cost)


@pytest.fixture(autouse=True)
def fake_world(monkeypatch):
    monkeypatch.setattr(insiders, "insiders", lambda: ROSTER)
    authors = {}

    def fake_oembed(pid, http=None):
        a = authors.get(pid)
        return None if a is None else {"author_url": f"https://x.com/{a}", "html": f"<blockquote><p>post {pid}</p></blockquote>"}

    monkeypatch.setattr(xsearch, "fetch_oembed", fake_oembed)
    monkeypatch.setenv("XAI_DAILY_BUDGET_USD", "1.0")
    monkeypatch.setenv("XAI_SWEEP_BUDGET_SHARE", "0.8")
    return authors


def test_for_teams_orders_nationals_first_and_skips_unverified():
    assert insiders.for_teams({"LV", "LA"}) == ["AdamSchefter", "RapSheet", "VBonsignore"]
    assert insiders.for_teams({"LV", "LA"}, include_unverified=True)[-1] == "JourdanRodrigue"


def seed_move(store, game):
    store.upsert_games([game])
    store.upsert_line_moves([{"move_id": "m1", "game_id": game["game_id"], "market_type": "winner",
                              "window_start": T0, "window_end": T0 + timedelta(minutes=20),
                              "magnitude_score": 1.5, "explain_status": "pending"}])


def test_search_moves_links_posts_with_lead_minutes(store, la_lv_game, fake_world):
    seed_move(store, la_lv_game)
    before, after, too_early = pid_at(T0 - timedelta(minutes=12)), pid_at(T0 + timedelta(minutes=25)), pid_at(T0 - timedelta(hours=3))
    fake_world.update({before: "AdamSchefter", after: "VBonsignore", too_early: "RapSheet"})
    client = FakeClient({"AdamSchefter": [before], "VBonsignore": [after], "RapSheet": [too_early]})
    assert search_moves.run(store, client, now=T0 + timedelta(hours=1)) == 1
    handles, start, end, focus = client.calls[0]
    assert handles == ("AdamSchefter", "RapSheet", "VBonsignore") and focus == "LA at LV"
    assert start == T0 - timedelta(minutes=60) and end == T0 + timedelta(minutes=20)
    links = {r["post_id"]: r["lead_minutes"] for r in store.move_posts("m1")}
    assert links == {before: 12.0}                     # after the window and too early are not linked
    assert len(store.posts_by_id([before, after, too_early])) == 3   # but all verified posts are stored
    assert store.line_moves()[0]["explain_status"] == "searched"
    run = store.search_runs("move")[0]
    assert run["move_id"] == "m1" and run["posts_kept"] == 3 and run["cost_estimate_usd"] == 0.1


def test_post_after_move_gets_negative_lead(store, la_lv_game, fake_world):
    seed_move(store, la_lv_game)
    during = pid_at(T0 + timedelta(minutes=5))
    fake_world[during] = "AdamSchefter"
    search_moves.run(store, FakeClient({"AdamSchefter": [during]}), now=T0 + timedelta(hours=1))
    assert store.move_posts("m1")[0]["lead_minutes"] == -5.0


def test_budget_stops_move_search_and_leaves_it_pending(store, la_lv_game):
    seed_move(store, la_lv_game)
    store.insert_search_run({"run_id": "old", "purpose": "sweep", "cost_estimate_usd": 1.0, "ran_at": T0})
    client = FakeClient({})
    assert search_moves.run(store, client, now=T0 + timedelta(hours=1)) == 0
    assert client.calls == [] and store.line_moves()[0]["explain_status"] == "pending"


def test_sweeps_stop_before_move_reserve(store):
    store.insert_search_run({"run_id": "old", "purpose": "move", "cost_estimate_usd": 0.8, "ran_at": T0})
    assert search_x.budget_left(store, "sweep", T0) == pytest.approx(0.0)
    assert search_x.budget_left(store, "move", T0) == pytest.approx(0.2)


def test_cooldown_reuses_posts_instead_of_searching(store, la_lv_game, fake_world):
    seed_move(store, la_lv_game)
    p = pid_at(T0 - timedelta(minutes=5))
    fake_world[p] = "AdamSchefter"
    store.upsert_posts([{"post_id": p, "author": "AdamSchefter", "posted_at": T0 - timedelta(minutes=5),
                         "url": "u", "text": "t", "text_source": "oembed"}])
    store.insert_search_run({"run_id": "r", "purpose": "move", "game_id": la_lv_game["game_id"],
                             "window_end": T0 + timedelta(minutes=30), "ran_at": T0 + timedelta(minutes=50),
                             "cost_estimate_usd": 0.1})
    client = FakeClient({})
    search_moves.run(store, client, now=T0 + timedelta(minutes=55))
    assert client.calls == [] and [r["post_id"] for r in store.move_posts("m1")] == [p]


def test_sweep_schedule_and_handles(store, la_lv_game):
    games = [la_lv_game]                                  # kickoff 20:25 UTC
    assert sweep_insiders.interval_minutes(datetime(2026, 10, 25, 19, 0), games) == 15
    assert sweep_insiders.interval_minutes(datetime(2026, 10, 25, 18, 0), games) == 60
    assert sweep_insiders.sweep_handles(datetime(2026, 10, 25, 12), games) == ["AdamSchefter", "RapSheet", "VBonsignore"]
    assert sweep_insiders.sweep_handles(datetime(2026, 10, 20, 12), games) == ["AdamSchefter", "RapSheet"]


def test_sweep_windows_chain_and_respect_due(store, la_lv_game, fake_world):
    store.upsert_games([la_lv_game])
    client = FakeClient({})
    now = datetime(2026, 10, 25, 12, 0)
    assert sweep_insiders.run(store, client, now=now) == 0
    assert client.calls[0][1] == now - sweep_insiders.MAX_LOOKBACK
    assert sweep_insiders.run(store, client, now=now + timedelta(minutes=30)) is None    # not due yet
    sweep_insiders.run(store, client, now=now + timedelta(minutes=61))
    assert client.calls[-1][1] == now                                                    # picks up where it left off


def test_setup_searches_do_not_count_against_daily_budget(store):
    store.insert_search_run({"run_id": "v", "purpose": "verify", "cost_estimate_usd": 5.0, "ran_at": T0})
    assert search_x.budget_left(store, "move", T0) == pytest.approx(1.0)
    assert search_x.budget_left(store, "verify", T0) == float("inf")
