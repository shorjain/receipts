from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from pipelines import train_model as tm


def test_no_vig_and_distance():
    assert tm.no_vig_home(-150, 130) == pytest.approx(0.6 / (0.6 + 100 / 230))
    assert tm.no_vig_home(None, 130) is None
    assert tm.km("NYG", "NYJ") == pytest.approx(0, abs=1)
    assert 3800 < tm.km("SEA", "MIA") < 4600


def test_rolling_features_exclude_the_current_game():
    sched = pd.DataFrame({"game_id": [f"g{i}" for i in range(5)],
                          "gameday": ["2025-09-07", "2025-09-14", "2025-09-21", "2025-09-28", "2025-10-05"],
                          "home_team": ["BUF"] * 5, "away_team": ["MIA"] * 5})
    tg = pd.DataFrame({"game_id": [f"g{i}" for i in range(4)], "team": ["BUF"] * 4,
                       "off_epa": [0.1, 0.3, 0.5, 9.9], "def_epa": [0.0] * 4, "off_sr": [.5] * 4,
                       "def_sr": [.4] * 4, "qb_epa": [.2] * 4})
    r = tm.rolling_team_features(sched, tg)
    r = r[r["team"] == "BUF"].set_index("game_id")
    assert np.isnan(r.loc["g1", "off_epa4"])                       # one prior game < min_periods
    assert r.loc["g2", "off_epa4"] == pytest.approx(0.2)            # mean of g0, g1 only
    assert r.loc["g3", "off_epa4"] == pytest.approx(0.3)            # g3's own 9.9 never leaks in
    # g4 hasn't been played (no stats): it still gets BUF's latest form
    assert r.loc["g4", "off_epa4"] == pytest.approx((0.1 + 0.3 + 0.5 + 9.9) / 4)


def test_injury_features_use_only_earlier_weeks():
    injuries = pd.DataFrame([{"season": 2025, "week": 3, "team": "BUF", "gsis_id": "q", "position": "QB",
                              "report_status": "Out"},
                             {"season": 2025, "week": 3, "team": "BUF", "gsis_id": "w", "position": "WR",
                              "report_status": "Questionable"}])
    snaps = pd.DataFrame([{"season": 2025, "week": 1, "pfr_player_id": "Q", "offense_pct": 1.0, "defense_pct": 0.0},
                          {"season": 2025, "week": 3, "pfr_player_id": "Q", "offense_pct": 0.0, "defense_pct": 0.0}])
    players = pd.DataFrame({"gsis_id": ["q", "w"], "pfr_id": ["Q", "W"]})
    f = tm.injury_features(injuries, snaps, players).iloc[0]
    assert (f["inj_off"], f["qb_out"]) == (1.0, 1.0)                # questionable players don't count


def frame(n_weeks=30):
    rng = np.random.default_rng(0)
    rows = []
    for w in range(n_weeks):
        for g in range(12):
            x = rng.normal()
            rows.append({"season": 2020 + w // 18, "week": w % 18 + 1, "game_id": f"{w}-{g}",
                         "kickoff_at": datetime(2020, 9, 1) + pd.Timedelta(days=7 * w),
                         "home_win": float(rng.random() < 1 / (1 + np.exp(-1.5 * x))), "close_prob": 0.5,
                         **{f: 0.0 for f in tm.FEATURES}, "d_off_epa8": x})
    return pd.DataFrame(rows)


def test_walk_forward_trains_only_on_earlier_games(monkeypatch):
    df = frame()
    seen = []
    real = tm.make_model

    def spy(name):
        m = real(name)
        fit = m.fit

        def wrapped(X, y):
            seen.append(df.loc[X.index, "kickoff_at"].max())
            return fit(X, y)
        m.fit = wrapped
        return m
    monkeypatch.setattr(tm, "make_model", spy)
    wf = tm.walk_forward(df, "logreg", [2021])
    assert len(wf) and all(t < datetime(2021, 1, 1) + pd.Timedelta(days=400) for t in seen)
    for (s, w), grp in wf.groupby(["season", "week"]):
        assert len(grp) == 12
    # the signal is learnable: the model beats a coin flip
    assert tm.log_loss(wf["home_win"], wf["model_prob"]) < tm.log_loss(wf["home_win"], [0.5] * len(wf))


def test_metrics_and_calibration():
    y, p = [1, 0, 1, 0], [0.9, 0.1, 0.6, 0.4]
    assert tm.brier(y, p) == pytest.approx((0.01 + 0.01 + 0.16 + 0.16) / 4)
    assert tm.log_loss(y, [0.5] * 4) == pytest.approx(np.log(2))
    cal = tm.calibration(y, p, bins=2)
    assert [c["n"] for c in cal] == [2, 2] and cal[1]["actual"] == 1.0
    sc = tm.score(pd.DataFrame({"home_win": y, "a": p, "b": [0.5, 0.5, None, 0.5]}), {"a": "a", "b": "b"})
    assert sc["n"] == 3                                             # common rows only
