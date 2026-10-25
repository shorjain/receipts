from datetime import datetime, timedelta

import pandas as pd
import pytest

from pipelines import extract_claims as ec
from pipelines import resolve_claims as rc
from pipelines import score_insiders as si
from pipelines.detect_moves import Point

T0 = datetime(2026, 10, 25, 15, 0)
GAMES = [{"game_id": "2026_07_CIN_BAL", "season": 2026, "week": 7, "home": "BAL", "away": "CIN",
          "kickoff_at": datetime(2026, 10, 25, 17, 0)},
         {"game_id": "2026_08_BAL_BUF", "season": 2026, "week": 8, "home": "BUF", "away": "BAL",
          "kickoff_at": datetime(2026, 11, 1, 18, 0)}]
ROSTER = [{"season": 2026, "week": 7, "team": "BAL", "player_id": "00-LJ", "full_name": "Lamar Jackson"},
          {"season": 2026, "week": 7, "team": "BAL", "player_id": "00-TH", "full_name": "Tyler Huntley"},
          {"season": 2026, "week": 7, "team": "DAL", "player_id": "00-GP", "full_name": "George Pickens"},
          {"season": 2026, "week": 7, "team": "NYG", "player_id": "00-J1", "full_name": "Josh Allen"},
          {"season": 2026, "week": 7, "team": "BUF", "player_id": "00-J2", "full_name": "Josh Allen"}]


# ---- extraction ------------------------------------------------------------------
def test_match_player_by_team_then_unique_global():
    idx = ec.roster_index(ROSTER)
    assert ec.match_player("Lamar Jackson", "BAL", idx) == ("00-LJ", "BAL")
    assert ec.match_player("Lamar Jakson", "BAL", idx) == ("00-LJ", "BAL")          # typo within team
    assert ec.match_player("George Pickens", "PIT", idx) == ("00-GP", "DAL")       # old team named
    assert ec.match_player("Josh Allen", None, idx) == (None, None)                # ambiguous: don't guess
    assert ec.match_player("Josh Allen", "BUF", idx) == ("00-J2", "BUF")
    assert ec.match_player("Nobody Real", "BAL", idx) == (None, "BAL")


def test_resolve_target_by_type():
    gid, rb = ec.resolve_target("inactive", "BAL", T0, "out", GAMES)
    assert gid == "2026_07_CIN_BAL" and rb == GAMES[0]["kickoff_at"] + timedelta(hours=4)
    assert ec.resolve_target("trade", "BAL", T0, "to NYJ", GAMES) == (None, T0 + timedelta(days=7))
    assert ec.resolve_target("return_date", "BAL", T0, "week 8", GAMES)[0] == "2026_08_BAL_BUF"
    assert ec.resolve_target("other", "BAL", T0, "x", GAMES) == (None, None)


class FakeAI:
    def __init__(self, reply):
        self.reply = reply

    def complete_json(self, prompt, schema):
        assert "[1] @jeffzrebiec" in prompt and "inactive" in prompt
        return self.reply


def test_extract_batch_maps_claims_and_skips_garbage():
    posts = [{"post_id": "p1", "author": "jeffzrebiec", "posted_at": T0, "text": "Ravens rule out Lamar Jackson."}]
    reply = {"claims": [
        {"post": 1, "player": "Lamar Jackson", "team": "Ravens", "claim_type": "inactive", "predicted_value": "out",
         "hedging": "confirmed", "credits_other": False, "credited_handle": "", "claim_text": "rule out Lamar"},
        {"post": 7, "player": "X", "team": "", "claim_type": "inactive", "predicted_value": "", "hedging": "confirmed",
         "credits_other": False, "credited_handle": "", "claim_text": ""},          # no such post
        {"post": 1, "player": "Y", "team": "", "claim_type": "made_up", "predicted_value": "", "hedging": "x",
         "credits_other": True, "credited_handle": "", "claim_text": ""}]}         # unknown type
    rows = ec.extract_batch(FakeAI(reply), posts, ec.roster_index(ROSTER), GAMES)
    assert len(rows) == 1
    r = rows[0]
    assert (r["player_id"], r["team"], r["game_id"], r["hedging"]) == ("00-LJ", "BAL", "2026_07_CIN_BAL", "confirmed")
    assert ec.extract_batch(FakeAI(None), posts, ec.roster_index(ROSTER), GAMES) == []


def test_stories_give_the_scoop_to_the_first_uncredited_post():
    def c(cid, author, minutes, credited=None, value="out"):
        return {"claim_id": cid, "author": author, "posted_at": T0 + timedelta(minutes=minutes), "player_id": "00-LJ",
                "player_name": "Lamar Jackson", "claim_type": "inactive", "predicted_value": value,
                "credited_handle": credited}
    claims = [c("a", "MikeGarafolo", 0, credited="RapSheet"), c("b", "RapSheet", 5), c("c", "AdamSchefter", 9),
              c("d", "AdamSchefter", 60 * 8)]   # 8 h later: a new story
    up = {u["claim_id"]: u for u in ec.assign_stories(claims)}
    assert [up[k]["is_original"] for k in "abcd"] == [False, True, False, True]
    assert up["a"]["story_id"] == up["b"]["story_id"] == up["c"]["story_id"] != up["d"]["story_id"]


# ---- resolution ------------------------------------------------------------------
def ctx():
    snaps = pd.DataFrame([
        {"game_id": "2026_07_CIN_BAL", "pfr_player_id": "HuntTy", "week": 7, "offense_snaps": 60, "defense_snaps": 0,
         "st_snaps": 0, "offense_pct": 1.0, "defense_pct": 0.0},
        {"game_id": "2026_07_CIN_BAL", "pfr_player_id": "BackUp", "week": 7, "offense_snaps": 10, "defense_snaps": 0,
         "st_snaps": 5, "offense_pct": 0.16, "defense_pct": 0.0}])
    players = pd.DataFrame([{"gsis_id": "00-LJ", "pfr_id": "JackLa", "position": "QB"},
                            {"gsis_id": "00-TH", "pfr_id": "HuntTy", "position": "QB"},
                            {"gsis_id": "00-BU", "pfr_id": "BackUp", "position": "WR"},
                            {"gsis_id": "00-GP", "pfr_id": "PickGe", "position": "WR"}])
    rosters = pd.DataFrame([{"gsis_id": "00-GP", "week": 6, "team": "PIT", "status": "ACT"},
                            {"gsis_id": "00-GP", "week": 7, "team": "DAL", "status": "ACT"},
                            {"gsis_id": "00-BU", "week": 7, "team": "BAL", "status": "ACT"}])
    pbp = pd.DataFrame([{"game_id": "2026_07_CIN_BAL", "play_id": 40, "posteam": "BAL", "pass_attempt": 1,
                         "passer_player_id": "00-TH"}])
    games = GAMES + [{"game_id": "2026_06_X", "season": 2026, "week": 6, "home": "PIT", "away": "CLE",
                      "kickoff_at": datetime(2026, 10, 18, 17)}]
    return rc.build_context(games, snaps, players, rosters, pbp)


def claim(ctype, pid, value="", game="2026_07_CIN_BAL", posted=T0, team="BAL", rb=None):
    return {"claim_type": ctype, "player_id": pid, "predicted_value": value, "game_id": game,
            "posted_at": posted, "team": team, "resolve_by": rb or GAMES[0]["kickoff_at"] + timedelta(hours=4)}


NOW = datetime(2026, 10, 27, 12)


def test_game_status_claims_use_snaps():
    c = ctx()
    assert rc.resolve(claim("inactive", "00-LJ"), c, NOW)[0] == "correct"           # no snaps
    assert rc.resolve(claim("active", "00-LJ"), c, NOW)[0] == "incorrect"
    assert rc.resolve(claim("questionable_will_play", "00-TH"), c, NOW)[0] == "correct"
    assert rc.resolve(claim("inactive", "00-TH"), c, NOW)[1]["snaps"] == 60


def test_unplayed_game_is_pending_then_unresolvable():
    c = ctx()
    future = claim("inactive", "00-LJ", game="2026_08_BAL_BUF", rb=GAMES[1]["kickoff_at"] + timedelta(hours=4))
    assert rc.resolve(future, c, NOW)[0] is None
    assert rc.resolve(future, c, GAMES[1]["kickoff_at"] + timedelta(days=12))[0] == "unresolvable"


def test_starter_qb_by_first_pass_and_others_by_snap_share():
    c = ctx()
    assert rc.resolve(claim("starter", "00-TH"), c, NOW)[0] == "correct"
    assert rc.resolve(claim("starter", "00-LJ"), c, NOW) == ("incorrect", {"table": "pbp", "game_id": "2026_07_CIN_BAL",
                                                                        "first_passer": "00-TH"})
    assert rc.resolve(claim("starter", "00-BU"), c, NOW)[0] == "incorrect"          # 16% of snaps


def test_roster_moves():
    c = ctx()
    posted = datetime(2026, 10, 20, 12)                                            # week 6
    assert rc.resolve(claim("trade", "00-GP", "to DAL", None, posted, "PIT", posted + timedelta(days=7)), c, NOW)[0] == "correct"
    assert rc.resolve(claim("trade", "00-GP", "to NYJ", None, posted, "PIT", posted + timedelta(days=7)), c, NOW)[0] == "incorrect"
    assert rc.resolve(claim("signing", "00-GP", "DAL", None, posted, "DAL", posted + timedelta(days=7)), c, NOW)[0] == "correct"


def test_unmatched_and_other_are_unresolvable():
    c = ctx()
    assert rc.resolve(claim("inactive", None), c, NOW)[0] == "unresolvable"
    assert rc.resolve(claim("other", "00-LJ"), c, NOW)[0] == "unresolvable"


# ---- scoring -----------------------------------------------------------------------
def scored(outcome, hedging="confirmed", **kw):
    return {"outcome": outcome, "hedging": hedging, "author": "a", "posted_at": T0, **kw}


def test_accuracy_is_smoothed_and_weighted():
    s = si.author_stats([scored("correct")] * 3)
    assert s["accuracy"] == pytest.approx(5 / 7, abs=1e-4) and s["raw_hit_rate"] == 1.0      # 3 lucky claims != 100%
    s = si.author_stats([scored("correct"), scored("incorrect")])                  # confirmed miss counts double
    assert s["accuracy"] == pytest.approx((1 + 2) / (3 + 4), abs=1e-4)
    s = si.author_stats([scored("correct", "rumor"), scored("incorrect", "rumor")])  # hedged: half weight
    assert s["accuracy"] == pytest.approx((0.5 + 2) / (1 + 4), abs=1e-4)
    s = si.author_stats([scored("unresolvable"), scored(None)])
    assert s["n_resolved"] == 0 and s["accuracy"] == 0.5 and s["raw_hit_rate"] is None


def test_original_ratio_scoops_and_leads():
    claims = [
        {**scored("correct"), "author": "a", "story_id": "s", "is_original": True, "posted_at": T0},
        {**scored("correct"), "author": "b", "story_id": "s", "is_original": False, "posted_at": T0 + timedelta(minutes=7)},
        {**scored(None), "author": "a", "credited_handle": "RapSheet", "story_id": "t", "is_original": False},
    ]
    si.add_story_leads(claims)
    s = si.author_stats([c for c in claims if c["author"] == "a"])
    assert s["original_ratio"] == 0.5 and s["scoop_rate"] == 1.0 and s["median_lead_minutes"] == 7.0


def test_credibility_weight_shrinks_toward_half():
    assert si.credibility_weight(None, None, None, None, 0) == 0.5
    few, many = si.credibility_weight(0.9, 1.0, 0.02, 1.0, 1), si.credibility_weight(0.9, 1.0, 0.02, 1.0, 50)
    assert 0.5 < few < many <= 1.0
    assert si.credibility_weight(0.2, 0.0, 0.0, 0.0, 50) < 0.3


def test_excess_move_subtracts_baseline_drift():
    pts = [Point(T0 - timedelta(hours=24) + timedelta(minutes=30 * i), 0.50 + (0.01 if i % 2 else 0), None)
           for i in range(48)]                                                     # wiggles 1 pp every 30 min
    pts += [Point(T0, 0.50, None), Point(T0 + timedelta(minutes=20), 0.56, None)]
    assert si.excess_move(pts, T0) == pytest.approx(0.06 - 0.01, abs=1e-3)


def test_scores_treat_pandas_nan_as_missing():
    nan = float("nan")
    s = si.author_stats([{**scored("correct"), "credited_handle": nan, "is_original": True, "market_move": nan}])
    assert s["original_ratio"] == 1.0 and s["scoop_rate"] == 1.0 and s["market_impact"] is None
