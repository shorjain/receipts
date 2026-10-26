"""Ask: plain-English questions, answered only from stored data, with sources."""
import streamlit as st

from app import ui
from rag.answer import answer, ask_backends

ui.header("Ask", "Ask about a line move, a reporter, a claim or a game. Answers use only what the app has stored, "
                 "and every sentence links to its source.", how="""
- Good questions name a **team**, **player**, **reporter** or **time**: "Why did the Bills line drop this morning?",
  "How accurate has Zrebiec been on injury calls this season?", "What does the model think about Titans at Bengals?"
- Each sentence ends with a source tag like [P2] (a post), [C1] (a claim), [M1] (a line move), [S1] (reporter
  stats) or [G1] (model prediction). Open **Sources** to see them.
- If the data doesn't cover your question, it says so instead of guessing.
""")

EXAMPLES = ["Who broke the Lamar Jackson news first?", "How accurate has Zrebiec been on injury calls this season?",
            "What does the model think about Titans at Bengals?", "Why did the Bills line drop this morning?"]


@st.cache_resource
def backends():
    return ask_backends()


if "history" not in st.session_state:
    st.session_state.history = []

st.html('<div class="rc-step">try one</div>')
cols = st.columns(len(EXAMPLES))
clicks = [c.button(ex, width="stretch") for c, ex in zip(cols, EXAMPLES)]   # draw every button first
picked = next((ex for ex, hit in zip(EXAMPLES, clicks) if hit), None)
question = st.chat_input("e.g. Who reported the Josh Jacobs news first?") or picked

if question:
    with st.spinner("Looking through stored moves, posts, claims and predictions…"):
        try:
            store, ai = backends()
            st.session_state.history.append(answer(question, store, ai))
        except Exception as e:  # keep the page usable if the AI backend hiccups
            st.cache_resource.clear()
            st.error(f"Couldn't answer right now: {e}")

KIND = {"M": "line move", "P": "reporter post", "C": "claim", "S": "reporter stats", "G": "model prediction"}
for a in reversed(st.session_state.history):
    with st.chat_message("user"):
        st.write(a.question)
    with st.chat_message("assistant"):
        st.write(a.text)
        for w in a.warnings:
            st.caption(f"Note: {w}")
        q = a.query
        if q:
            bits = ([", ".join(ui.nick(t) for t in q.teams)] if q.teams else []) + \
                   [p["full_name"] for p in q.players] + ["@" + h for h in q.insiders] + ([q.window_label] if q.window_label else [])
            if bits:
                st.caption("Looked for: " + " · ".join(bits))
        if a.evidence:
            with st.expander(f"Sources ({len(a.evidence)})"):
                for e in a.evidence:
                    link = f" · [open on X ↗]({e['url']})" if e.get("url") else ""
                    st.markdown(f"**[{e['id']}]** *{KIND.get(e['kind'], '')}* · {e['line']}{link}")
