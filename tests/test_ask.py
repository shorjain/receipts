from datetime import datetime, timedelta

import pytest

from rag import answer as ans
from rag.parse import parse

NOW = datetime(2026, 10, 25, 18, 0)        # Sunday 2 PM ET
SEASON = datetime(2026, 8, 1)
ROSTER = [{"player_id": "00-LJ", "full_name": "Lamar Jackson", "team": "BAL"},
          {"player_id": "00-TK", "full_name": "Travis Kelce", "team": "KC"},
          {"player_id": "00-JA1", "full_name": "Josh Allen", "team": "BUF"},
          {"player_id": "00-JA2", "full_name": "Josh Allen", "team": "JAX"}]
INSIDERS = [{"handle": "DMRussini", "name": "Dianna Russini"}, {"handle": "AkronJackson", "name": "Zac Jackson"},
            {"handle": "AdamSchefter", "name": "Adam Schefter"}]


def p(text, weeks=None):
    return parse(text, NOW, ROSTER, INSIDERS, SEASON, weeks or {})


def test_brief_examples_parse():
    q = p("Why did the Bills line drop this morning?")
    assert q.teams == ["BUF"] and q.intent == "moves" and q.window_label == "this morning"
    assert q.start == datetime(2026, 10, 25, 9, 0) and q.end == datetime(2026, 10, 25, 16, 0)   # 5 AM-noon ET
    q = p("Who broke the Kelce news first?")
    assert [x["full_name"] for x in q.players] == ["Travis Kelce"] and q.intent == "scoop"
    q = p("How accurate has Russini been on injury calls this season?")
    assert q.insiders == ["DMRussini"] and q.intent == "scores" and "inactive" in q.claim_types
    assert q.window_label == "this season" and q.start == SEASON


def test_player_last_name_does_not_pull_in_a_same_named_insider():
    q = p("Who broke the Lamar Jackson news first?")
    assert [x["full_name"] for x in q.players] == ["Lamar Jackson"] and q.insiders == []
    assert p("What has Jackson reported lately?").insiders == ["AkronJackson"]   # bare insider surname


def test_ambiguous_names_and_abbreviations():
    assert p("Is Allen playing?").players == []                     # two Josh Allens, no team
    assert p("no news for the Saints").teams == ["NO"]              # lowercase "no" isn't New Orleans
    assert p("NO at TB odds").teams == ["NO", "TB"]
    assert p("Giants vs Jets").teams == ["NYG", "NYJ"]
    assert p("New York line moves").teams == []                     # ambiguous city


def test_windows():
    assert p("moves yesterday").window_label == "yesterday"
    q = p("line moves in the last 3 hours")
    assert q.start == NOW - timedelta(hours=3)
    weeks = {7: (datetime(2026, 10, 18), datetime(2026, 10, 27))}
    assert p("week 7 moves", weeks).start == datetime(2026, 10, 18)
    assert p("anything on Friday?").start == datetime(2026, 10, 23, 4, 0)   # Friday 00:00 ET


class FakeAI:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, prompt, temperature=0.0):
        self.prompts.append(prompt)
        return self.reply


def seed(store):
    store.upsert_games([{"game_id": "2026_07_CIN_BAL", "season": 2026, "week": 7, "home": "BAL", "away": "CIN",
                         "kickoff_at": datetime(2026, 10, 25, 17)}])
    store.upsert_posts([{"post_id": "1", "author": "jeffzrebiec", "posted_at": datetime(2026, 10, 24, 19, 31),
                         "text": "Ravens rule QB Lamar Jackson (ankle) out for Sunday.", "url": "https://x.com/a/status/1",
                         "text_source": "oembed"}])
    store.upsert_claims([
        {"claim_id": "c1", "post_id": "1", "author": "jeffzrebiec", "posted_at": datetime(2026, 10, 24, 19, 31),
         "player_id": "00-LJ", "player_name": "Lamar Jackson", "team": "BAL", "claim_type": "inactive",
         "predicted_value": "out", "hedging": "confirmed", "is_original": True},
        {"claim_id": "c2", "post_id": "2", "author": "jamisonhensley", "posted_at": datetime(2026, 10, 24, 19, 37),
         "player_id": "00-LJ", "player_name": "Lamar Jackson", "team": "BAL", "claim_type": "inactive",
         "predicted_value": "out", "hedging": "confirmed", "is_original": False}])


def test_scoop_question_is_answered_from_claims_and_guarded(store):
    seed(store)
    ai = FakeAI("Jeff Zrebiec reported it first at 3:31 PM ET [C1], six minutes before Jamison Hensley [C2]. "
                "He had 40 sources [C1].")
    a = ans.answer("Who broke the Lamar Jackson news first?", store, ai, now=NOW, roster=ROSTER, insider_rows=INSIDERS)
    assert not a.refused and [e["id"] for e in a.evidence[:2]] == ["C1", "C2"] or "C1" in {e["id"] for e in a.evidence}
    assert a.text.startswith("Jeff Zrebiec reported it first") and "40 sources" not in a.text
    assert "first among tracked accounts" in ai.prompts[0]


def test_no_evidence_refuses_without_calling_the_model(store):
    seed(store)
    ai = FakeAI("unused")
    a = ans.answer("Why did the Cardinals line move last week?", store, ai, now=NOW, roster=ROSTER, insider_rows=INSIDERS)
    assert a.refused and a.text == ans.NO_DATA and ai.prompts == []


def test_ungrounded_reply_becomes_a_refusal(store):
    seed(store)
    a = ans.answer("Who broke the Lamar Jackson news first?", store, FakeAI("Adam Schefter had it 3 hours earlier."),
                   now=NOW, roster=ROSTER, insider_rows=INSIDERS)
    assert a.refused and a.text == ans.NO_DATA
