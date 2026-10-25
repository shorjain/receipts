"""Moves: live feed of line moves with grounded explanations and cited posts."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import data  # noqa: E402
from common import teams  # noqa: E402

st.set_page_config(page_title="Moves · NFL Lines", page_icon="🏈", layout="wide")
st.title("Why did the line move?")

days = st.sidebar.slider("Days back", 1, 30, 7)
mv, gm = data.moves(days), data.games()
if mv.empty:
    st.info("No moves detected in this period yet. Moves appear when a line jumps 3+ points "
            "of win probability, crosses a key spread number, or the total moves 1.5+.")
    st.stop()

mv = mv.merge(gm[["game_id", "week", "home", "away", "kickoff_at"]], on="game_id", how="left")
weeks = sorted(mv["week"].dropna().astype(int).unique())
week = st.sidebar.multiselect("Week", weeks, default=weeks)
team = st.sidebar.selectbox("Team", ["All"] + sorted(teams.TEAMS))
venue = st.sidebar.selectbox("Venue", ["All", "kalshi", "polymarket", "pinnacle", "consensus"])
status = st.sidebar.radio("Explanation", ["All", "Explained", "Unexplained", "Pending"])

f = mv[mv["week"].isin(week)]
if team != "All":
    f = f[(f["home"] == team) | (f["away"] == team)]
if venue != "All":
    f = f[f["venues"].fillna("").str.contains(venue)]
if status == "Explained":
    f = f[f["explain_status"] == "explained"]
elif status == "Unexplained":
    f = f[f["explain_status"].isin(["unexplained", "refused"])]
elif status == "Pending":
    f = f[f["explain_status"].isin(["pending", "searched"])]
f = f.sort_values("window_start", ascending=False)
st.caption(f"{len(f)} of {len(mv)} moves")


def fmt_change(r) -> str:
    if r["market_type"] == "winner":
        return f"{r['home']} win {r['prob_before']:.1%} → {r['prob_after']:.1%} ({(r['prob_after'] - r['prob_before']) * 100:+.1f} pp)"
    if r["market_type"] == "spread":
        s = f"{r['home']} spread {r['line_before']:+.1f} → {r['line_after']:+.1f}"
        return s + (f" · crossed {int(r['crossed_key_number'])}" if pd.notna(r["crossed_key_number"]) else "")
    return f"Total {r['line_before']:.1f} → {r['line_after']:.1f}"


BADGE = {"explained": "🟢 explained", "unexplained": "⚪ unexplained", "refused": "🟠 no grounded explanation",
         "searched": "⏳ explaining", "pending": "⏳ waiting for posts"}

for _, r in f.iterrows():
    with st.container(border=True):
        top = st.columns([3, 2, 1])
        top[0].markdown(f"**{r['away']} @ {r['home']}** · {r['market_type']} · week {int(r['week']) if pd.notna(r['week']) else '?'}")
        top[1].markdown(fmt_change(r))
        top[2].markdown(f"{BADGE.get(r['explain_status'], r['explain_status'])}  \n"
                        f"{'🔁 multi-venue' if r['venue_scope'] == 'multi' else 'single venue'} · ×{r['magnitude_score']:.1f}")
        st.caption(f"{r['window_start']:%a %b %d %H:%M}–{r['window_end']:%H:%M} UTC · venues: {r['venues']}"
                   + (f" · {r['leader_venue']} moved first by {r['lead_lag_minutes']:g} min"
                      if pd.notna(r.get("leader_venue")) and pd.notna(r.get("lead_lag_minutes")) else ""))
        if r.get("explanation"):
            st.markdown(r["explanation"])

        posts = data.move_posts(r["move_id"])
        left, right = st.columns([3, 2])
        with left:
            s = data.series(r["game_id"], r["market_type"], r["window_start"] - timedelta(minutes=90),
                            r["window_end"] + timedelta(minutes=60))
            if not s.empty:
                y = "value:Q"
                base = alt.Chart(s).mark_line(interpolate="step-after").encode(
                    x=alt.X("t:T", title=None), color=alt.Color("venue:N", legend=alt.Legend(orient="bottom")),
                    y=alt.Y(y, title="P(home win)" if r["market_type"] == "winner" else "line",
                            scale=alt.Scale(zero=False)))
                band = alt.Chart(pd.DataFrame([{"a": r["window_start"], "b": r["window_end"]}])).mark_rect(
                    opacity=0.12).encode(x="a:T", x2="b:T")
                layers = [band, base]
                if not posts.empty:
                    pr = alt.Chart(posts).mark_rule(strokeDash=[3, 3], color="gray").encode(
                        x="posted_at:T", tooltip=["author", "posted_at", "lead_minutes"])
                    layers.append(pr)
                st.altair_chart(alt.layer(*layers).properties(height=180), use_container_width=True)
        with right:
            if posts.empty:
                st.caption("No tracked-insider posts linked to this move.")
            else:
                cited = posts.sort_values("attribution_score", ascending=False, na_position="last").head(5)
                for _, p in cited.iterrows():
                    when = (f"{p['lead_minutes']:g} min before" if p["lead_minutes"] >= 0
                            else f"{-p['lead_minutes']:g} min after")
                    score = f" · attribution {p['attribution_score']:.2f}" if pd.notna(p["attribution_score"]) else ""
                    st.markdown(f"**@{p['author']}** · {when}{score}  \n{(p['text'] or '')[:280]}  \n[open on X]({p['url']})")
