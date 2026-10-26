"""Game odds: our model's win chances next to the market's, and how good the model really is."""
import json

import altair as alt
import pandas as pd
import streamlit as st

from app import data, ui
from common.settings import env_float

ui.header("Game odds", "Upcoming games: what the betting market thinks, next to what our own model thinks.", how="""
- **Market** is the win chance from Kalshi (or Polymarket, or sportsbooks) when the model last ran.
- **Our model** uses only stats known before kickoff: each team's recent efficiency on offense and defense,
  quarterback play, rest, travel and injuries. It can't see breaking news, weather or betting money.
- **A big gap** (8+ points) is flagged. With this model, a gap is more often the model missing news than the
  market being wrong. Check the report card at the bottom before trusting it.
""")
THRESH = env_float("MODEL_DISAGREE_POINTS", 0.08)
LABELS = {"d_off_epa4": "recent offense", "d_off_epa8": "offense over 8 games", "d_def_epa4": "recent defense",
          "d_def_epa8": "defense over 8 games", "d_off_sr8": "offensive consistency", "d_def_sr8": "defensive consistency",
          "d_qb_epa8": "quarterback play", "rest_diff": "extra rest", "away_travel": "away team's travel",
          "neutral": "neutral field", "d_inj_off": "offensive injuries", "d_inj_def": "defensive injuries",
          "d_qb_out": "starting QB out"}

live, gm = data.predictions("-live"), data.games()
if live.empty:
    st.info("The model hasn't run yet. It retrains every 6 hours.")
else:
    latest = live.sort_values("made_at").groupby("game_id").tail(1)
    df = latest.merge(gm[["game_id", "home", "away", "kickoff_at", "week"]], on="game_id")
    df = df[df["kickoff_at"] > pd.Timestamp.now("UTC").tz_localize(None)].sort_values("kickoff_at")
    st.caption(f"{len(df)} upcoming games · model last ran {ui.ago(latest['made_at'].max())}")
    cols = st.columns(2)
    for i, (_, r) in enumerate(df.iterrows()):
        home, away, p = ui.nick(r["home"]), ui.nick(r["away"]), r["home_win_prob"]
        mk = r["market_prob_at_time"]
        with cols[i % 2].container(border=True):
            st.html(f'<div class="rc-big">{away} at {home}</div>'
                    f'<div class="rc-step">{ui.et(r["kickoff_at"])}</div>')
            a, b = st.columns(2)
            a.metric(f"market: {home} win", f"{mk:.0%}" if pd.notna(mk) else "—")
            b.metric(f"our model: {home} win", f"{p:.0%}",
                     f"{(p - mk) * 100:+.0f} pts vs market" if pd.notna(mk) else None, delta_color="off", delta_arrow="off")
            if pd.notna(mk) and abs(p - mk) >= THRESH:
                lean = home if p > mk else away
                st.html(f'<div class="rc-callout">⚠️ Big disagreement: the model likes the {lean} more than the market '
                        f'does. Usually the market knows something the model can\'t see.</div>')
            feats = r["features_json"]
            feats = json.loads(feats) if isinstance(feats, str) else (feats or {})
            contrib = feats.get("_contributions")
            if contrib:
                top = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:3]
                why = ", ".join(f"{LABELS.get(k, k)} ({'favors ' + home if v > 0 else 'favors ' + away})" for k, v in top)
                st.caption(f"Biggest factors for the model: {why}.")

st.subheader("How good is our model?")
rep = data.model_report()
if not rep:
    st.info("No report yet.")
    st.stop()
best = rep["best_model"]
vc = rep["models"][best]["vs_close"]
beat = vc["model"]["log_loss"] < vc["close"]["log_loss"]
seasons = f"{min(rep['test_seasons'])}–{max(rep['test_seasons'])}"
if beat:
    st.success(f"Over {vc['n']} past games ({seasons}), our model was slightly more accurate than the betting "
               f"market's closing odds.")
else:
    st.html(f'<div class="rc-callout">Over <b>{vc["n"]} past games</b> ({seasons}), the betting market\'s closing odds '
            f'were <b>more accurate than our model</b>. That\'s normal: the market sees injuries, weather and betting '
            f'money the model doesn\'t. Treat the model as a second opinion, not a pick.</div>')
k = rep["models"][best].get("vs_kalshi")
c = st.columns(3)
c[0].metric("our model (error score)", f"{vc['model']['log_loss']:.3f}", help="Log loss: lower is better. A coin flip scores 0.693.")
c[1].metric("closing odds (error score)", f"{vc['close']['log_loss']:.3f}")
if k and k["n"]:
    c[2].metric(f"Kalshi, 1h before kickoff ({k['n']} games)", f"{k['kalshi']['log_loss']:.3f}")
st.caption("Error score = log loss (lower is better; a coin flip scores 0.693). Each past week was predicted by a "
           "model trained only on games before it, so this is how it would really have done.")

cal = pd.DataFrame([{**b, "who": "our model" if who == "model" else "closing odds"}
                    for who in ("model", "close") for b in vc[who]["calibration"]])
diag = alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]})).mark_line(strokeDash=[4, 4], color="#5a5a5a").encode(x="x", y="y")
pts = alt.Chart(cal).mark_line(point=True).encode(
    x=alt.X("predicted:Q", title="predicted chance the home team wins", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%")),
    y=alt.Y("actual:Q", title="how often they actually won", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%")),
    color=alt.Color("who:N", title=None, scale=alt.Scale(range=["#9a9a9a", "#f5f5f5"])),
    tooltip=["bin", "n", alt.Tooltip("predicted:Q", format=".0%"), alt.Tooltip("actual:Q", format=".0%")])
st.altair_chart(ui.chart_theme((diag + pts).properties(height=320,
                title="Is it honest? Points on the dotted line mean '60% chance' really wins 60% of the time")), width="stretch")
