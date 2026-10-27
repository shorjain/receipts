"""receipts: Streamlit entry point.  Run:  streamlit run app/Home.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app import ui  # noqa: E402

static = Path(__file__).parent / "static"
st.set_page_config(page_title=ui.APP_NAME, page_icon=str(static / "icon.svg"), layout="wide",
                   initial_sidebar_state="collapsed")
st.html(ui.CSS)
live = ui.live_data_warning()
if live:
    st.warning(live)
st.logo(str(static / "logo.svg"), size="large")

views = Path(__file__).parent / "views"
pages = [
    st.Page(views / "home.py", title="Home", default=True),
    st.Page(views / "moves.py", title="Line moves", url_path="moves"),
    st.Page(views / "scorecard.py", title="Insider scorecard", url_path="scorecard"),
    st.Page(views / "insider.py", title="Insider receipts", url_path="insider"),
    st.Page(views / "games.py", title="Game odds", url_path="games"),
    st.Page(views / "ask.py", title="Ask", url_path="ask"),
]
st.navigation(pages, position="top").run()   # a top bar, like a site header
