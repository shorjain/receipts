"""Home: what this is, what it's doing right now, and where to look."""
import pandas as pd
import streamlit as st

from app import data, ui

st.html(f'<div class="rc-hero"><h1>{ui.APP_NAME}</h1><p>{ui.TAGLINE}</p>'
        '<p style="font-size:.98rem;color:#9a9a9a;max-width:760px;margin:6px auto 0">'
        'Watches NFL betting markets around the clock. When a game\'s odds suddenly jump, it finds the '
        'reporter post that most likely caused it. It also keeps score on those reporters: '
        'every claim they make gets checked against what actually happened.</p></div>')

stats = data.status()
age = data.snapshot_age_minutes()
week = data.moves(7)
found = int(week.apply(ui.cause_status, axis=1).eq("found").sum()) if len(week) else 0
c = st.columns(4)
c[0].metric("last odds check", ui.ago(stats["last_price_at"]))
c[1].metric("line moves this week", f"{len(week)}", f"{found} with a clear cause", delta_color="off", delta_arrow="off")
c[2].metric("insider claims tracked", f"{stats['claims'] or 0}",
            f"{stats['claims_checked'] or 0} checked so far", delta_color="off", delta_arrow="off")
c[3].metric("reporters followed", f"{stats['insiders'] or 0}", f"{stats['posts'] or 0} posts read", delta_color="off", delta_arrow="off")
if age is None:
    st.warning("No data yet. Start the collector: `python -m scripts.service install`.")
elif age > 15:
    st.warning(f"The data hasn't refreshed in {age:.0f} minutes. Is the collector running? "
               "Check with `python -m scripts.service status`.")

st.subheader("How it works")
steps = [
    ("01 · watch", "Watch the markets",
     "Every minute it reads win odds, spreads and totals for every game from Kalshi and Polymarket, "
     "plus sportsbooks a few times a day."),
    ("02 · explain", "Find the cause",
     "When odds jump, it searches what tracked NFL reporters posted on X just before, and writes a short "
     "explanation that only uses those posts. If nobody posted first, it says so."),
    ("03 · keep score", "Keep the receipts",
     "Every checkable claim (\"X is out\", \"Y will start\") is checked against official stats after the game, "
     "so you can see which reporters are actually right."),
]
cols = st.columns(3)
for col, (step, title, body) in zip(cols, steps):
    col.html(f'<div class="rc-card"><div class="rc-step">{step}</div><h3>{title}</h3><p>{body}</p></div>')

st.subheader("Explore")
links = [
    ("moves", "Line moves", "Every sudden odds change, what probably caused it, and the posts behind it."),
    ("scorecard", "Insider scorecard", "Which reporters are right, who's first, and whose posts move the market."),
    ("games", "Game odds", "Our model's win chances next to the market's, for every upcoming game."),
    ("ask", "Ask", "Ask in plain English: \"Who broke the Lamar Jackson news first?\""),
]
cols = st.columns(4)
for col, (path, title, body) in zip(cols, links):
    with col:
        st.html(f'<div class="rc-card"><h3>{title}</h3><p>{body}</p></div>')
        st.page_link(f"views/{path}.py", label=f"Open {title.lower()} ↗")

st.subheader("Latest line moves")
mv, gm = data.moves(7), data.games()
if mv.empty:
    st.caption("No line moves in the last week yet.")
else:
    mv = mv.merge(gm[["game_id", "home", "away", "kickoff_at"]], on="game_id", how="left").dropna(subset=["home"])
    for _, m in mv.sort_values("window_start", ascending=False).head(4).iterrows():
        head, cls = ui.move_sentence(m)
        found = {"found": "cause found", "none": "no insider news first", "unclear": "no clear cause",
                 "pending": "still looking"}[ui.cause_status(m)]
        st.html(f'<div class="rc-post"><div class="rc-big {cls}" style="font-size:1.05rem">{head}</div>'
                f'<div class="who">{ui.et(m["window_start"])} · {ui.until_kickoff(m["window_start"], m["kickoff_at"])} · '
                f'{found}</div></div>')

with st.expander("New to betting markets? Quick glossary"):
    st.markdown(ui.GLOSSARY)
