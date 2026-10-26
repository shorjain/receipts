from datetime import datetime, timedelta

from common.relevance import RelevanceIndex, about_game
from pipelines import search_moves

ROSTER = [
    {"player_id": "1", "full_name": "Patrick Mahomes", "team": "KC", "week": 7},
    {"player_id": "2", "full_name": "Bo Nix", "team": "DEN", "week": 7},
    {"player_id": "3", "full_name": "Cam Skattebo", "team": "NYG", "week": 7},
    {"player_id": "4", "full_name": "Josh Allen", "team": "BUF", "week": 7},
    {"player_id": "5", "full_name": "Josh Allen", "team": "JAX", "week": 7},
    {"player_id": "6", "full_name": "Courtland Sutton", "team": "DEN", "week": 7},
    {"player_id": "7", "full_name": "Tyreek Hill", "team": "MIA", "week": 7},
    {"player_id": "8", "full_name": "Taysom Hill", "team": "NO", "week": 7},
]
INSIDERS = [{"handle": "ZacStevens", "tier": "beat", "teams": ["DEN"]},
            {"handle": "RapSheet", "tier": "national", "teams": []}]
GAME = {"game_id": "2026_08_DEN_KC", "season": 2026, "home": "KC", "away": "DEN"}
T = RelevanceIndex(ROSTER, INSIDERS).terms(GAME)


def test_the_reported_bug_skattebo_is_not_about_den_kc():
    assert not about_game("Giants RB Cam Skattebo is expected to play Sunday, per sources.", "RapSheet", T)
    assert not about_game("Skattebo is good to go.", "RapSheet", T)


def test_team_names_cities_hashtags_and_abbreviations():
    assert about_game("Chiefs WR is inactive today", "RapSheet", T)
    assert about_game("Big news for #ChiefsKingdom", "RapSheet", T)
    assert about_game("Kansas City will be without its starting LT", "RapSheet", T)
    assert about_game("DEN injury report is out", "RapSheet", T)
    assert about_game("BRONCOS SIGN A KICKER", "RapSheet", T)
    assert not about_game("Somebody has to pay the bills", "RapSheet", T)          # lowercase word, not a team
    assert not about_game("The chiefs of staff met today", "RapSheet", T)


def test_players_by_full_name_or_league_unique_surname():
    assert about_game("Patrick Mahomes is a full participant", "RapSheet", T)
    assert about_game("Sutton (hamstring) won't practice", "RapSheet", T)           # unique surname
    assert about_game("Bo Nix threw for 300", "RapSheet", T)
    assert not about_game("Hill is questionable", "RapSheet", T)                    # two Hills, neither here


def test_beat_reporters_count_without_naming_the_team():
    assert about_game("Sean Payton said the plan hasn't changed.", "ZacStevens", T)


def test_link_rows_drops_posts_about_other_games():
    t0 = datetime(2026, 10, 25, 18)
    move = {"move_id": "m", "window_start": t0, "window_end": t0 + timedelta(minutes=20)}
    posts = [{"post_id": "a", "author": "RapSheet", "posted_at": t0 - timedelta(minutes=10), "text": "Cam Skattebo will play"},
             {"post_id": "b", "author": "RapSheet", "posted_at": t0 - timedelta(minutes=5), "text": "Chiefs rule out a WR"}]
    rows = search_moves.link_rows(move, posts, GAME, RelevanceIndex(ROSTER, INSIDERS))
    assert [r["post_id"] for r in rows] == ["b"]
