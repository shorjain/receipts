"""Leaderboard: which insiders can you trust? Every score links to its receipts."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import data  # noqa: E402
from common import teams  # noqa: E402
from pipelines.score_insiders import compute_scores  # noqa: E402

st.set_page_config(page_title="Leaderboard · NFL Lines", page_icon="🏈", layout="wide")
st.title("Insider credibility leaderboard")
st.caption("Accuracy is smoothed, (correct + 2) / (resolved + 4), so a few lucky calls don't top the board. "
           "Hedged claims count half; confirmed misses count double. Click an insider for every receipt.")

cl = data.claims()
if cl.empty:
    st.info("No claims extracted yet. The loop extracts claims from stored posts every hour.")
    st.stop()

cl["season"] = cl["posted_at"].apply(lambda t: t.year if t.month >= 3 else t.year - 1)
season = st.sidebar.selectbox("Season", sorted(cl["season"].unique(), reverse=True))
window = st.sidebar.radio("Window", ["season", "last4w"], format_func=lambda w: {"season": "Season",
                                                                                  "last4w": "Rolling 4 weeks"}[w])
types = st.sidebar.multiselect("Claim types", sorted(cl["claim_type"].unique()), default=sorted(cl["claim_type"].unique()))
team = st.sidebar.selectbox("Team", ["All"] + sorted(teams.TEAMS))

f = cl[(cl["season"] == season) & cl["claim_type"].isin(types)]
if team != "All":
    f = f[f["team"] == team]
now = datetime.now(timezone.utc).replace(tzinfo=None)
rows = [r for r in compute_scores(f.to_dict("records"), now, int(season)) if r["score_window"] == window]
if not rows:
    st.info("No claims match these filters.")
    st.stop()
sc = pd.DataFrame(rows)
meta, earned = data.insider_meta(), data.badges(sc)
sc["name"] = sc["author"].map(lambda a: meta.get(a, {}).get("name", a))
sc["outlet"] = sc["author"].map(lambda a: meta.get(a, {}).get("outlet", ""))
sc["badges"] = sc["author"].map(lambda a: " ".join(earned.get(a, [])))
sc["receipts"] = "/Insider?author=" + sc["author"]
sc["hit"] = sc.apply(lambda r: f"{r['raw_hit_rate']:.0%} of {r['n_resolved']}" if pd.notna(r["raw_hit_rate"]) else "—", axis=1)
sc = sc.sort_values(["credibility_weight", "n_resolved"], ascending=False)

st.dataframe(
    sc[["receipts", "name", "outlet", "credibility_weight", "accuracy", "hit", "n_claims", "scoop_rate",
        "median_lead_minutes", "market_impact", "original_ratio", "badges"]],
    hide_index=True, width="stretch", height=min(800, 40 + 35 * len(sc)),
    column_config={
        "receipts": st.column_config.LinkColumn("Insider", display_text=r"author=(.*)"),
        "credibility_weight": st.column_config.ProgressColumn("Credibility", min_value=0, max_value=1, format="%.2f"),
        "accuracy": st.column_config.NumberColumn("Accuracy", format="%.2f"),
        "hit": "Raw hit rate",
        "n_claims": "Claims",
        "scoop_rate": st.column_config.NumberColumn("Scoop rate", format="%.2f"),
        "median_lead_minutes": st.column_config.NumberColumn("Lead (min)", format="%.1f"),
        "market_impact": st.column_config.NumberColumn("Market impact", format="%.3f",
                                                       help="Mean excess win-probability move in the 30 min after their claims"),
        "original_ratio": st.column_config.NumberColumn("Original", format="%.2f"),
    })
pending = int(f["outcome"].isna().sum())
st.caption(f"{len(f)} claims · {pending} waiting for game data (snap counts are published a day or two after games) · "
           f"{int((f['outcome'] == 'unresolvable').sum())} can't be checked and don't count.")
