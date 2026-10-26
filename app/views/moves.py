"""Line moves: every sudden odds change, its likely cause, and the posts behind it."""
from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from app import data, ui
from common import teams

ui.header("Line moves", "When a game's odds suddenly change, here's what changed, when, and what probably caused it.",
          how="""
- **What counts as a move:** a team's win chance changes 3+ points within 30 minutes, the spread crosses
  3, 7, 10 or 14, or the expected total moves 1.5+ points. Only moves before kickoff count.
- **Confirmed on 2 markets** means Kalshi and Polymarket (or the sportsbooks) moved together, a strong sign of real news.
- **What probably happened** is written only from the reporter posts shown under it. Every sentence points
  to a post like [P1]. If no tracked reporter posted before the market moved, it says so: that usually
  means sharp bettors, or news from someone the app doesn't follow.
- **The chart** shows each market's odds around the move; the shaded band is the move, and dotted lines
  mark when reporters posted.
""")

mv, gm = data.moves(30), data.games()
if mv.empty:
    st.info("No line moves yet. They show up here as soon as a market jumps.")
    st.stop()
mv = mv.merge(gm[["game_id", "week", "home", "away", "kickoff_at"]], on="game_id", how="left").dropna(subset=["home"])

f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
show = f1.segmented_control("Show", ["All", "Cause found", "No cause found"], default="All")
days = f2.selectbox("Period", [1, 3, 7, 14, 30], index=2, format_func=lambda d: f"last {d} day{'s' if d > 1 else ''}")
team = f3.selectbox("Team", ["All teams"] + sorted(teams.TEAMS, key=lambda t: ui.nick(t)),
                    format_func=lambda t: t if t == "All teams" else f"{ui.nick(t)} ({t})")
kind = f4.selectbox("Market", ["All", "Win chance", "Spread", "Total"])

now = pd.Timestamp.now("UTC").tz_localize(None)
f = mv[mv["window_start"] >= now - pd.Timedelta(days=days)]
if team != "All teams":
    f = f[(f["home"] == team) | (f["away"] == team)]
if kind != "All":
    f = f[f["market_type"] == {"Win chance": "winner", "Spread": "spread", "Total": "total"}[kind]]
f = f.assign(cause=f.apply(ui.cause_status, axis=1))
if show == "Cause found":
    f = f[f["cause"] == "found"]
elif show == "No cause found":
    f = f[f["cause"].isin(["none", "unclear"])]
f = f.sort_values("window_start", ascending=False)
st.caption(f"{len(f)} moves")

STATUS = {"found": ("What probably happened", True),
          "unclear": ("No clear cause: reporters posted around this time, but not about anything that explains it", True),
          "none": ("No tracked reporter posted about this before the market moved. That often means sharp "
                   "bettors, or news from someone the app doesn't follow.", False),
          "pending": ("Searching reporter posts and writing an explanation…", False)}

for _, m in f.head(40).iterrows():
    with st.container(border=True):
        head, cls = ui.move_sentence(m)
        st.html(f'<div class="rc-big {cls}">{head}</div>')
        tags = [ui.tag(ui.et(m["window_start"])), ui.tag(ui.until_kickoff(m["window_start"], m["kickoff_at"])),
                ui.tag(f"week {int(m['week'])}" if pd.notna(m["week"]) else "")]
        n_venues = len([v for v in (m["venues"] or "").split(",") if v])
        tags.append(ui.tag("confirmed on 2 markets", on=True) if n_venues > 1 else ui.tag(f"seen on {ui.venues_text(m['venues'])}"))
        tags.append(ui.tag(f"{ui.size_word(m['magnitude_score'])} move"))
        if pd.notna(m.get("leader_venue")) and pd.notna(m.get("lead_lag_minutes")) and m["lead_lag_minutes"]:
            tags.append(ui.tag(f"{ui.venues_text(m['leader_venue'])} moved {m['lead_lag_minutes']:g} min first"))
        st.html("".join(t for t in tags if t))

        label, has_text = STATUS.get(m["cause"], ("", False))
        if has_text and m.get("explanation"):
            st.html(f'<div class="rc-step" style="margin-top:8px">{label.lower()}</div>'
                    f'<div class="rc-callout">{m["explanation"]}</div>')
        elif label:
            st.html(f'<div class="rc-callout rc-muted">{label}</div>')

        posts = data.move_posts(m["move_id"])
        left, right = st.columns([3, 2])
        with left:
            s = data.series(m["game_id"], m["market_type"], m["window_start"] - timedelta(minutes=90),
                            m["window_end"] + timedelta(minutes=60))
            if not s.empty:
                s["venue"] = s["venue"].map(lambda v: ui.VENUE_NAMES.get(v, v))
                s["when"] = s["t"].dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.tz_localize(None)
                ytitle = "win chance" if m["market_type"] == "winner" else "spread" if m["market_type"] == "spread" else "total"
                base = alt.Chart(s).mark_line(interpolate="step-after", strokeWidth=2).encode(
                    x=alt.X("when:T", title=None, axis=alt.Axis(format="%-I:%M %p")),
                    y=alt.Y("value:Q", title=ytitle, scale=alt.Scale(zero=False),
                            axis=alt.Axis(format=".0%" if m["market_type"] == "winner" else "g")),
                    color=alt.Color("venue:N", title=None, scale=alt.Scale(range=["#f5f5f5", "#8a8a8a", "#4ade80", "#f87171"])))
                band_df = pd.DataFrame([{"a": m["window_start"], "b": m["window_end"]}])
                for col in ("a", "b"):
                    band_df[col] = pd.to_datetime(band_df[col]).dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.tz_localize(None)
                layers = [alt.Chart(band_df).mark_rect(opacity=0.10, color="#f5f5f5").encode(x="a:T", x2="b:T"), base]
                if not posts.empty:
                    p = posts.copy()
                    p["when"] = pd.to_datetime(p["posted_at"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.tz_localize(None)
                    layers.append(alt.Chart(p).mark_rule(strokeDash=[3, 3], color="#9a9a9a").encode(
                        x="when:T", tooltip=["author", "lead_minutes"]))
                st.altair_chart(ui.chart_theme(alt.layer(*layers).properties(height=190)), width="stretch")
        with right:
            if posts.empty:
                st.caption("No tracked reporter posts around this move.")
            else:
                st.html('<div class="rc-step">what reporters posted</div>')
                # Same five posts, in the same order, that the explanation cites as [P1]..[P5].
                top = posts.sort_values("attribution_score", ascending=False, na_position="last").head(5)
                for i, (_, p) in enumerate(top.sort_values("posted_at").iterrows(), 1):
                    lead = p["lead_minutes"]
                    when = f"{lead:g} min before the move" if lead >= 0 else f"{-lead:g} min after the move started"
                    text = (p["text"] or "")[:240]
                    st.html(f'<div class="rc-post"><div class="who">[P{i}] @{p["author"]} · {when}</div>'
                            f'<div style="margin:4px 0">{text}</div><a href="{p["url"]}" target="_blank">open on X ↗</a></div>')
