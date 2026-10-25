"""Ask: grounded answers about moves, insiders, claims and the model, with the evidence shown."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st  # noqa: E402

from rag.answer import answer, ask_backends  # noqa: E402

st.set_page_config(page_title="Ask · NFL Lines", page_icon="🏈", layout="wide")
st.title("Ask")
st.caption("Answers come only from stored data, and every sentence cites its evidence. "
           "When the data doesn't cover a question, it says so instead of guessing.")

EXAMPLES = ["Why did the Bills line drop this morning?", "Who broke the Lamar Jackson news first?",
            "How accurate has Zrebiec been on injury calls this season?",
            "What does the model think about Titans at Bengals?"]


@st.cache_resource
def backends():
    return ask_backends()


if "history" not in st.session_state:
    st.session_state.history = []

cols = st.columns(len(EXAMPLES))
picked = None
for c, ex in zip(cols, EXAMPLES):
    if c.button(ex, width="stretch"):
        picked = ex
question = st.chat_input("Ask about a line move, an insider, a claim or a game") or picked

if question:
    with st.spinner("Searching stored moves, posts, claims and predictions…"):
        store, ai = backends()
        try:
            a = answer(question, store, ai)
        except Exception as e:  # keep the chat usable if Snowflake hiccups
            st.cache_resource.clear()
            st.error(f"Couldn't answer right now: {e}")
            a = None
    if a:
        st.session_state.history.append(a)

for a in reversed(st.session_state.history):
    with st.chat_message("user"):
        st.write(a.question)
    with st.chat_message("assistant"):
        st.write(a.text)
        q = a.query
        if q:
            bits = [f"teams {', '.join(q.teams)}" if q.teams else "", f"players {', '.join(p['full_name'] for p in q.players)}" if q.players else "",
                    f"insiders {', '.join('@' + h for h in q.insiders)}" if q.insiders else "",
                    q.window_label, f"looking for {q.intent}" if q.intent != "any" else ""]
            st.caption("Understood as: " + " · ".join(b for b in bits if b))
        if a.evidence:
            with st.expander(f"Evidence ({len(a.evidence)})"):
                for e in a.evidence:
                    link = f" · [open on X]({e['url']})" if e.get("url") else ""
                    st.markdown(f"**[{e['id']}]** {e['line']}{link}")
