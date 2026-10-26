"""Insider receipts: every claim one reporter made, and whether it held up."""
import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app import data, ui
from pipelines.score_insiders import compute_scores

cl = data.claims()
if cl.empty:
    ui.header("Insider receipts", "Every claim a reporter made, and whether it held up.")
    st.info("No claims yet.")
    st.stop()
meta_all = data.insider_meta()
authors = sorted(cl["author"].unique(), key=lambda a: meta_all.get(a, {}).get("name", a).lower())
want = st.query_params.get("author")
author = st.selectbox("Reporter", authors, index=authors.index(want) if want in authors else 0,
                      format_func=lambda a: f"{meta_all.get(a, {}).get('name', a)} (@{a})")
st.query_params["author"] = author
meta = meta_all.get(author, {})
covers = f" · covers {', '.join(ui.nick(t) for t in meta['teams'])}" if meta.get("teams") else ""
ui.header(meta.get("name", author), f"@{author} · {meta.get('outlet', '')}{covers}")

mine = cl[cl["author"] == author].sort_values("posted_at", ascending=False)
now = datetime.now(timezone.utc).replace(tzinfo=None)
row = next((r for r in compute_scores(mine.to_dict("records"), now, now.year) if r["score_window"] == "season"), None)
right = int((mine["outcome"] == "correct").sum())
wrong = int((mine["outcome"] == "incorrect").sum())
waiting = int(mine["outcome"].isna().sum())
c = st.columns(4)
c[0].metric("trust score", f"{row['credibility_weight'] * 100:.0f}" if row else "50", "starts at 50", delta_color="off", delta_arrow="off")
c[1].metric("right calls", f"{right} of {right + wrong}" if right + wrong else "none checked yet")
c[2].metric("first to report", f"{row['scoop_rate']:.0%}" if row and row["scoop_rate"] is not None else "—",
            help="Of their own claims, how often they were first among tracked reporters")
c[3].metric("claims tracked", len(mine), f"{waiting} waiting for game stats", delta_color="off", delta_arrow="off")

PHRASE = {"inactive": "won't play", "active": "will play", "questionable_will_play": "will play despite being questionable",
          "starter": "will start", "release": "is being released", "other": ""}


def claim_text(r) -> str:
    who = r["player_name"] or "A player"
    team = f" ({r['team']})" if r["team"] else ""
    v = r["predicted_value"] or ""
    if r["claim_type"] == "trade":
        what = f"is being traded {v}".strip()
    elif r["claim_type"] == "signing":
        what = f"is signing {v}".strip()
    elif r["claim_type"] == "return_date":
        what = f"will return {v}".strip()
    else:
        what = PHRASE.get(r["claim_type"], "") or v
    return f"{who}{team} {what}"


def checked_how(e, outcome) -> str:
    if e is None or (isinstance(e, float) and pd.isna(e)):
        return "game stats not out yet"
    e = json.loads(e) if isinstance(e, str) else e
    if "reason" in e:
        return {"player not matched to an nflverse ID": "couldn't identify the player",
                "no checkable data": "nothing official to check against"}.get(e["reason"], e["reason"])
    if e.get("table") == "snap_counts" and "snaps" in e:
        return f"played {e['snaps']:g} snaps" if e["snaps"] else "didn't play a snap"
    if e.get("table") == "snap_counts" and "snap_share" in e:
        return f"played {e['snap_share']:.0%} of snaps"
    if e.get("table") == "pbp":
        return "threw the team's first pass" if outcome == "correct" else "someone else threw the first pass"
    if e.get("table") == "rosters_weekly":
        return "weekly roster changed" if outcome == "correct" else "roster didn't change in time"
    return ""


RESULT = {"correct": "✅ right", "incorrect": "❌ wrong", "unresolvable": "➖ can't check"}
SURE = {"confirmed": "said for sure", "expected": "expected", "rumor": "rumor"}

st.subheader(f"Receipts · {len(mine)} claims")
log = pd.DataFrame({
    "When": mine["posted_at"].map(ui.et),
    "Claim": mine.apply(claim_text, axis=1),
    "How sure": mine["hedging"].map(SURE).fillna(""),
    "First?": mine.apply(lambda r: f"credited @{r['credited_handle']}" if isinstance(r["credited_handle"], str)
                         else ("first" if r["is_original"] is True else "after others"), axis=1),
    "Result": mine["outcome"].map(RESULT).fillna("⏳ waiting"),
    "How we checked": [checked_how(e, o) for e, o in zip(mine["evidence"], mine["outcome"])],
    "Their words": mine["claim_text"].fillna(""),
    "Post": mine["url"],
})
st.dataframe(log, hide_index=True, width="stretch", height=min(900, 42 + 35 * len(log)),
             column_config={"Post": st.column_config.LinkColumn("Post", display_text="open on X ↗")})
