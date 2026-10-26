from datetime import datetime, timedelta

import pytest

from pipelines import explain_moves as em

T0 = datetime(2026, 10, 25, 14, 5)


class FakeAI:
    def __init__(self, answer, sims=None):
        self.answer, self.sims, self.prompts = answer, sims, []

    def similarities(self, query, texts):
        return self.sims or [0.85] * len(texts)

    def complete(self, prompt, temperature=0.0):
        self.prompts.append(prompt)
        return self.answer


@pytest.mark.parametrize("lead,w", [(0, 1.0), (20, 1.0), (40, 0.5), (60, 0.0), (90, 0.0)])
def test_timing_weight_before(lead, w):
    assert em.timing_weight(lead) == pytest.approx(w)


def test_timing_weight_after_decays_toward_zero():
    assert em.timing_weight(-1) < 0.3 and em.timing_weight(-30) < 0.02


def test_relevance_rescale_and_bonus():
    assert em.relevance(0.65, False) == 0.0 and em.relevance(0.90, False) == 1.0
    assert em.relevance(0.775, True) == pytest.approx(0.65)
    assert em.relevance(0.95, True) == 1.0


def test_mentions_team_or_player(la_lv_game):
    assert em.mentions("Raiders WR out", la_lv_game, [])
    assert em.mentions("Matthew Stafford inactive", la_lv_game, ["Matthew Stafford"])
    assert not em.mentions("Chiefs sign a punter", la_lv_game, ["Matthew Stafford"])


def seed(store, game, posts):
    store.upsert_games([game])
    store.upsert_line_moves([{"move_id": "m1", "game_id": game["game_id"], "market_type": "winner",
                              "window_start": T0, "window_end": T0 + timedelta(minutes=25),
                              "prob_before": 0.40, "prob_after": 0.45, "explain_status": "searched",
                              "summary_text": "Los Angeles Rams at Las Vegas Raiders: Las Vegas Raiders win "
                                              "probability 40.0% -> 45.0% (+5.0 pp), 2026-10-25 14:05-14:30 UTC, "
                                              "single-venue (kalshi)."}])
    for pid, author, lead, text in posts:
        t = T0 - timedelta(minutes=lead)
        store.upsert_posts([{"post_id": pid, "author": author, "posted_at": t, "text": text,
                             "url": f"https://x.com/{author}/status/{pid}", "text_source": "oembed"}])
        store.upsert_move_posts([{"move_id": "m1", "post_id": pid, "lead_minutes": float(lead)}])


def test_explained_move_is_grounded_and_scored(store, la_lv_game):
    seed(store, la_lv_game, [("1", "AdamSchefter", 12, "Rams QB Matthew Stafford is inactive today."),
                             ("2", "VinnyBonsignore", -5, "Raiders line moving fast.")])
    ai = FakeAI("Raiders win probability rose from 40.0% to 45.0% [M1] after Schefter reported Stafford "
                "inactive [P1]. The line moved 9 points. Bonsignore's post caused it [P2].")
    assert em.run(store, ai, now=T0 + timedelta(hours=1)) == 1
    m = store.line_moves()[0]
    assert m["explain_status"] == "explained"
    assert m["explanation"] == ("Raiders win probability rose from 40.0% to 45.0% [M1] after Schefter "
                                "reported Stafford inactive [P1].")
    links = {r["post_id"]: r for r in store.move_posts("m1")}
    assert links["1"]["attribution_score"] > links["2"]["attribution_score"] > 0
    assert "AFTER the move started" in ai.prompts[0] and "[M1]" in ai.prompts[0]


def test_no_post_before_move_is_unexplained_without_ai_call(store, la_lv_game):
    seed(store, la_lv_game, [("2", "VinnyBonsignore", -5, "Raiders line moving fast.")])
    ai = FakeAI("unused")
    em.run(store, ai, now=T0 + timedelta(hours=1))
    m = store.line_moves()[0]
    assert m["explain_status"] == "unexplained" and m["explanation"] == em.UNEXPLAINED and ai.prompts == []


def test_ungrounded_answer_is_refused(store, la_lv_game):
    seed(store, la_lv_game, [("1", "AdamSchefter", 12, "Rams QB Matthew Stafford is inactive today.")])
    em.run(store, FakeAI("Sharp bettors moved it 12 points because of weather."), now=T0 + timedelta(hours=1))
    m = store.line_moves()[0]
    assert m["explain_status"] == "refused" and m["explanation"].startswith("No grounded explanation")


def test_grounded_but_unclear_answers_get_their_own_status(store, la_lv_game):
    seed(store, la_lv_game, [("1", "AdamSchefter", 12, "Rams QB Matthew Stafford is inactive today.")])
    em.run(store, FakeAI("The cause of the move from 40.0% to 45.0% [M1] is unclear from tracked insiders."),
           now=T0 + timedelta(hours=1))
    assert store.line_moves()[0]["explain_status"] == "unclear"
