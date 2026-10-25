"""Games: the model's win probability next to the market's, and how the model really scores."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import data  # noqa: E402
from common.settings import env_float  # noqa: E402

st.set_page_config(page_title="Games · NFL Lines", page_icon="🏈", layout="wide")
st.title("Model vs market")
THRESH = env_float("MODEL_DISAGREE_POINTS", 0.08)

LABELS = {"d_off_epa4": "offense EPA, last 4", "d_off_epa8": "offense EPA, last 8", "d_def_epa4": "defense EPA, last 4",
          "d_def_epa8": "defense EPA, last 8", "d_off_sr8": "offense success rate", "d_def_sr8": "defense success rate",
          "d_qb_epa8": "QB EPA per dropback", "rest_diff": "rest days", "away_travel": "away travel",
          "neutral": "neutral site", "d_inj_off": "offensive injuries", "d_inj_def": "defensive injuries",
          "d_qb_out": "starting QB out"}

live, gm = data.predictions("-live"), data.games()
if live.empty:
    st.info("No predictions yet. The loop trains the model every 6 hours "
            "(or run `python -m pipelines.train_model`).")
else:
    latest = live.sort_values("made_at").groupby("game_id").tail(1)
    df = latest.merge(gm[["game_id", "home", "away", "kickoff_at", "week"]], on="game_id")
    df = df[df["kickoff_at"] > pd.Timestamp.utcnow().tz_localize(None)].sort_values("kickoff_at")
    df["gap"] = df["home_win_prob"] - df["market_prob_at_time"]
    st.caption(f"P(home win). Market = Kalshi (else Polymarket, else book consensus) when the prediction was made. "
               f"⚠️ marks gaps of {THRESH:.0%} or more; with this model, a gap is more often the model missing "
               f"news than the market being wrong (see the report card below).")
    for _, r in df.iterrows():
        flag = "⚠️ " if pd.notna(r["gap"]) and abs(r["gap"]) >= THRESH else ""
        mk = f"{r['market_prob_at_time']:.0%}" if pd.notna(r["market_prob_at_time"]) else "—"
        gap = f"{r['gap'] * 100:+.0f} pts" if pd.notna(r["gap"]) else ""
        with st.expander(f"{flag}{r['away']} @ {r['home']} · {r['kickoff_at']:%a %b %d %H:%M} UTC · "
                         f"model {r['home_win_prob']:.0%} · market {mk} {gap}"):
            feats = r["features_json"]
            feats = json.loads(feats) if isinstance(feats, str) else (feats or {})
            contrib = feats.get("_contributions")
            if contrib:
                top = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:5]
                st.markdown("**What moved the model** (log-odds toward the home team):")
                for k, v in top:
                    st.markdown(f"- {LABELS.get(k, k)}: {v:+.2f}")
            st.caption("Inputs: " + ", ".join(f"{LABELS.get(k, k)} {v:+.2f}" for k, v in feats.items()
                                               if not k.startswith("_") and v is not None))

st.header("Report card")
rep = data.model_report()
if not rep:
    st.info("No model report yet.")
    st.stop()
best = rep["best_model"]
st.caption(f"Walk-forward by week over seasons {', '.join(map(str, rep['test_seasons']))}: every week is predicted by a "
           f"model fit only on earlier games. Lower log loss and Brier are better. Live model: **{best}**.")
rows = []
for name, m in rep["models"].items():
    for bench, sc in m.items():
        for who, v in sc.items():
            if isinstance(v, dict):
                rows.append({"model": name, "comparison": bench.replace("vs_", "vs "), "probabilities": who,
                             "games": sc["n"], "log loss": v["log_loss"], "Brier": v["brier"]})
tbl = pd.DataFrame(rows)
st.dataframe(tbl, hide_index=True, width="stretch")
vc = rep["models"][best]["vs_close"]
beat = vc["model"]["log_loss"] < vc["close"]["log_loss"]
(st.success if beat else st.warning)(
    f"{'Beats' if beat else 'Does not beat'} the closing line: log loss {vc['model']['log_loss']:.3f} vs "
    f"{vc['close']['log_loss']:.3f} over {vc['n']} games. The close is the real benchmark; "
    + ("" if beat else "expect the market to know injuries, weather and lineup news this model can't see."))

cal = []
for who in ("model", "close"):
    for b in vc[who]["calibration"]:
        cal.append({**b, "who": "model" if who == "model" else "closing line"})
cal = pd.DataFrame(cal)
line = alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]})).mark_line(strokeDash=[4, 4], color="gray").encode(x="x", y="y")
pts = alt.Chart(cal).mark_line(point=True).encode(
    x=alt.X("predicted:Q", title="predicted P(home win)", scale=alt.Scale(domain=[0, 1])),
    y=alt.Y("actual:Q", title="actual home win rate", scale=alt.Scale(domain=[0, 1])),
    color="who:N", tooltip=["bin", "n", "predicted", "actual"])
st.altair_chart((line + pts).properties(height=320, title="Calibration"), width="stretch")
st.caption("The closing line is nflverse's consensus close with the vig removed (Pinnacle's close would need paid "
           "Odds API history).")
