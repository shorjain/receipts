"""Insider scorecard: which reporters are right, who's first, and whose posts move markets."""
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app import data, ui
from common import teams
from pipelines.score_insiders import compute_scores

ui.header("Insider scorecard", "Which NFL reporters are actually right, and who gets the news first.", how="""
- Every checkable claim a reporter posts ("X is out Sunday", "Y will start") is checked against official
  NFL stats after the game: did the player take a snap, who threw the first pass, did the roster change.
- **Trust score (0–100)** blends how often they're right (most of the weight), how often they're first,
  how much the odds move after their posts, and how much is their own reporting. Everyone starts at
  **50**, and the score moves away from 50 only as their claims get checked, so a couple of lucky
  calls can't top the board.
- **Right calls** counts checked claims only. Claims about games that haven't happened, or that official
  stats can't confirm, aren't counted.
- Hedged claims ("expected to", "could") count half; a confident claim that turns out wrong counts double.
- Click a name to see every claim they made: the receipts.
""")

cl = data.claims()
if cl.empty:
    st.info("No claims yet. Claims appear as reporters post and the app reads their posts.")
    st.stop()

TYPES = {"Injury news": ["active", "inactive", "questionable_will_play", "return_date"], "Who starts": ["starter"],
         "Trades & signings": ["trade", "signing", "release"], "Other": ["other"]}
cl["season"] = cl["posted_at"].apply(lambda t: t.year if t.month >= 3 else t.year - 1)
f1, f2, f3, f4 = st.columns(4)
season = f1.selectbox("Season", sorted(cl["season"].unique(), reverse=True))
window = f2.segmented_control("Period", ["Season", "Last 4 weeks"], default="Season")
kinds = f3.multiselect("Claim types", list(TYPES), default=list(TYPES))
team = f4.selectbox("Team", ["All teams"] + sorted(teams.TEAMS, key=ui.nick),
                    format_func=lambda t: t if t == "All teams" else f"{ui.nick(t)} ({t})")

types = [t for k in kinds for t in TYPES[k]]
f = cl[(cl["season"] == season) & cl["claim_type"].isin(types)]
if team != "All teams":
    f = f[f["team"] == team]
now = datetime.now(timezone.utc).replace(tzinfo=None)
win_key = "season" if window != "Last 4 weeks" else "last4w"
rows = [r for r in compute_scores(f.to_dict("records"), now, int(season)) if r["score_window"] == win_key]
if not rows:
    st.info("No claims match these filters.")
    st.stop()
sc = pd.DataFrame(rows)
meta, earned = data.insider_meta(), data.badges(sc)
checked = f[f["outcome"].isin(["correct", "incorrect"])].groupby("author")["outcome"]
right = checked.apply(lambda s: (s == "correct").sum()).to_dict()
total = checked.size().to_dict()

out = pd.DataFrame({
    "Reporter": "/insider?author=" + sc["author"],
    "Outlet": sc["author"].map(lambda a: meta.get(a, {}).get("outlet", "")),
    "Trust score": (sc["credibility_weight"] * 100).round(0),
    "Right calls": sc["author"].map(lambda a: f"{right.get(a, 0)} of {total.get(a, 0)} checked" if total.get(a) else "none checked yet"),
    "First to report": sc["scoop_rate"].map(lambda v: f"{v:.0%}" if pd.notna(v) else "—"),
    "Claims": sc["n_claims"],
    "Badges": sc["author"].map(lambda a: " ".join(earned.get(a, []))),
}).sort_values(["Trust score", "Claims"], ascending=False)

n_checked = int(f["outcome"].isin(["correct", "incorrect"]).sum())
waiting = int(f["outcome"].isna().sum())
if n_checked < 20:
    st.html(f'<div class="rc-callout">Early days: {n_checked} of {len(f)} claims have been checked so far, so most '
            f'scores are still near 50. {waiting} claims are waiting for game stats, which usually arrive a day or '
            f'two after the game.</div>')

st.dataframe(out, hide_index=True, width="stretch", height=min(760, 42 + 35 * len(out)), column_config={
    "Reporter": st.column_config.LinkColumn("Reporter", display_text=r"author=(.*)", help="Click for every claim they made"),
    "Trust score": st.column_config.ProgressColumn("Trust score", min_value=0, max_value=100, format="%d",
                                                   help="0–100. Everyone starts at 50."),
    "First to report": st.column_config.TextColumn(help="Of their own claims, how often they were first among tracked reporters"),
})

with st.expander("Detailed numbers"):
    st.dataframe(sc[["author", "accuracy", "raw_hit_rate", "n_resolved", "n_claims", "scoop_rate", "median_lead_minutes",
                     "market_impact", "original_ratio", "credibility_weight"]].rename(columns={
        "author": "reporter", "accuracy": "accuracy (smoothed)", "raw_hit_rate": "raw hit rate", "n_resolved": "checked",
        "median_lead_minutes": "minutes ahead of next reporter", "market_impact": "odds move after their posts",
        "original_ratio": "own reporting share", "credibility_weight": "trust (0–1)"}),
        hide_index=True, width="stretch")
    st.caption("Accuracy (smoothed) = (right + 2) / (checked + 4), with hedged claims at half weight and confident "
               "misses at double. The formulas are in the README.")
