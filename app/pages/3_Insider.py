"""Insider: one reporter's full receipt log. The receipts are the point."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import data  # noqa: E402
from pipelines.score_insiders import compute_scores  # noqa: E402

st.set_page_config(page_title="Insider · NFL Lines", page_icon="🏈", layout="wide")
cl = data.claims()
if cl.empty:
    st.info("No claims extracted yet.")
    st.stop()
authors = sorted(cl["author"].unique(), key=str.lower)
want = st.query_params.get("author")
author = st.sidebar.selectbox("Insider", authors, index=authors.index(want) if want in authors else 0)
st.query_params["author"] = author
meta = data.insider_meta().get(author, {})
st.title(f"{meta.get('name', author)}  ·  @{author}")
st.caption(f"{meta.get('outlet', '')} · {meta.get('tier', '')}"
           + (f" · covers {', '.join(meta['teams'])}" if meta.get("teams") else ""))

mine = cl[cl["author"] == author].sort_values("posted_at", ascending=False)
now = datetime.now(timezone.utc).replace(tzinfo=None)
row = next((r for r in compute_scores(mine.to_dict("records"), now, now.year) if r["score_window"] == "season"), None)
if row:
    c = st.columns(6)
    c[0].metric("Credibility", f"{row['credibility_weight']:.2f}")
    c[1].metric("Accuracy", f"{row['accuracy']:.2f}",
                help="(weighted correct + 2) / (weighted resolved + 4)")
    c[2].metric("Raw hit rate", f"{row['raw_hit_rate']:.0%}" if row["raw_hit_rate"] is not None else "—",
                f"{row['n_resolved']} resolved of {row['n_claims']}", delta_color="off")
    c[3].metric("Scoop rate", f"{row['scoop_rate']:.0%}" if row["scoop_rate"] is not None else "—")
    c[4].metric("Lead", f"{row['median_lead_minutes']:.0f} min" if row["median_lead_minutes"] is not None else "—")
    c[5].metric("Original", f"{row['original_ratio']:.0%}" if row["original_ratio"] is not None else "—")

ICON = {"correct": "✅ correct", "incorrect": "❌ incorrect", "unresolvable": "➖ can't check"}


def evidence(e) -> str:
    if e is None or (isinstance(e, float) and pd.isna(e)):
        return "waiting for game data"
    e = json.loads(e) if isinstance(e, str) else e
    if "reason" in e:
        return e["reason"]
    if e.get("table") == "snap_counts" and "snaps" in e:
        return f"snap_counts {e['game_id']}: {e['snaps']:g} snaps"
    if e.get("table") == "snap_counts" and "snap_share" in e:
        return f"snap_counts {e['game_id']}: {e['snap_share']:.0%} of snaps"
    if e.get("table") == "pbp":
        return f"pbp {e['game_id']}: first pass by {e['first_passer']}"
    return json.dumps(e)[:120]


st.subheader(f"Receipts ({len(mine)})")
log = pd.DataFrame({
    "posted": mine["posted_at"].dt.strftime("%b %d %H:%M"),
    "claim": mine["claim_type"].str.replace("_", " "),
    "player": mine["player_name"].fillna("—") + mine["team"].fillna("").map(lambda t: f" ({t})" if t else ""),
    "predicted": mine["predicted_value"].fillna(""),
    "hedging": mine["hedging"],
    "original": mine.apply(lambda r: f"credits @{r['credited_handle']}" if isinstance(r["credited_handle"], str)
                           else ("scoop" if r["is_original"] is True else "followed"), axis=1),
    "outcome": mine["outcome"].map(ICON).fillna("⏳ pending"),
    "evidence": mine["evidence"].map(evidence),
    "quote": mine["claim_text"].fillna(""),
    "post": mine["url"],
})
st.dataframe(log, hide_index=True, width="stretch", height=min(900, 40 + 35 * len(log)),
             column_config={"post": st.column_config.LinkColumn("Post", display_text="open on X")})
