"""NFL Lines: Streamlit entry point.  Run:  streamlit run app/Home.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app import data  # noqa: E402

st.set_page_config(page_title="NFL Lines", page_icon="🏈", layout="wide")
st.title("NFL Lines")
st.caption("Why did the line move, and which insiders can you trust?")

age = data.snapshot_age_minutes()
if age is None:
    st.warning("No data snapshot yet. Start the loop: `python -m pipelines.ingest_odds --loop` "
               "(it writes data/ui_snapshot.duckdb every 5 minutes).")
else:
    st.caption(f"Data as of {age:.0f} min ago (the ingest loop refreshes it every 5 minutes).")

mv = data.moves(7)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Moves (7 days)", len(mv))
c2.metric("Explained", int((mv["explain_status"] == "explained").sum()) if len(mv) else 0)
c3.metric("Unexplained", int((mv["explain_status"] == "unexplained").sum()) if len(mv) else 0)
c4.metric("Multi-venue", int((mv["venue_scope"] == "multi").sum()) if len(mv) else 0)

st.markdown("""
**Pages**
- **Moves**: every detected line move with the insider posts that most likely caused it.
- **Leaderboard**: insiders ranked by accuracy, speed and market impact.
- **Insider**: one reporter's full receipt log, every claim with its outcome and evidence.
- **Games**: the model's win probability next to the market's, with an honest report card.
- Ask arrives in a later milestone.
""")
