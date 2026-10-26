"""Shared look and plain-language helpers for every page.

Visual language: near-black background with a faint grid, Oxanium headings, Inter body
text, monospace labels and buttons, thin borders and square corners, monochrome with
green/red only where a number goes up or down.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from common import teams

APP_NAME = "receipts"
TAGLINE = "Why the line moved. Who called it first."
ET = ZoneInfo("America/New_York")
UP, DOWN, MUTED = "#4ade80", "#f87171", "#9a9a9a"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Oxanium:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
:root { --bg:#0d0d0d; --panel:#141414; --line:#2e2e2e; --text:#f5f5f5; --muted:#9a9a9a; --up:#4ade80; --down:#f87171; }
.stApp {
  background-color: var(--bg);
  background-image:
    linear-gradient(to right, rgba(245,245,245,.06) 1px, transparent 1px),
    linear-gradient(rgba(245,245,245,.06) 1px, transparent 1px);
  background-size: 48px 48px;
}
/* Text only: a blanket [class*="st-"] rule would also replace the Material icon font */
html, body, .stMarkdown, .stMarkdown p, .stMarkdown li, p, li, label, input, textarea,
[data-testid="stWidgetLabel"], [data-testid="stDataFrame"] { font-family: Inter, -apple-system, sans-serif; }
[data-testid="stIconMaterial"], .material-symbols-rounded, span[class*="material"] { font-family: 'Material Symbols Rounded' !important; }
h1, h2, h3, h4 { font-family: Oxanium, Inter, sans-serif !important; letter-spacing: -0.03em; color: var(--text); }
h1 { font-weight: 700 !important; }
[data-testid="stSidebar"] { background: #0a0a0a; border-right: 1px solid var(--line); }
header[data-testid="stHeader"] { background: #0d0d0d; border-bottom: 1px solid var(--line); }
header[data-testid="stHeader"] a, header[data-testid="stHeader"] button p { font-family: 'JetBrains Mono', monospace; }
.stButton button, .stLinkButton a, [data-testid="stBaseButton-secondary"] {
  font-family: 'JetBrains Mono', monospace !important; border-radius: 0 !important;
  border: 1px solid rgba(240,240,240,.44) !important; background: transparent !important; color: var(--text) !important;
}
.stButton button:hover, .stLinkButton a:hover { background: var(--text) !important; color: var(--bg) !important; }
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"] details, div[data-testid="stMetric"] {
  border-radius: 0 !important; border-color: var(--line) !important; background: rgba(20,20,20,.85);
}
div[data-testid="stMetric"] { border: 1px solid var(--line); padding: 14px 16px; }
div[data-testid="stMetricValue"] { font-family: Oxanium, sans-serif; }
div[data-testid="stMetricLabel"] p { font-family: 'JetBrains Mono', monospace; font-size: .78rem; color: var(--muted); text-transform: lowercase; }
[data-testid="stCaptionContainer"], .stCaption { color: var(--muted) !important; }
code { font-family: 'JetBrains Mono', monospace !important; }
.rc-brand { font-family: Oxanium, sans-serif; font-weight: 700; font-size: 1.6rem; letter-spacing: -0.04em; }
.rc-hero { text-align:center; padding: 28px 0 8px; }
.rc-hero h1 { font-size: 3rem; margin-bottom: .2rem; }
.rc-hero p { color: #d4d4d4; font-size: 1.15rem; }
.rc-tag { display:inline-block; font-family:'JetBrains Mono',monospace; font-size:.74rem; padding:2px 8px;
  border:1px solid var(--line); color: var(--muted); margin-right:6px; }
.rc-tag.on { color: var(--text); border-color: rgba(240,240,240,.44); }
.rc-card { border:1px solid var(--line); background: rgba(20,20,20,.9); padding: 18px 20px; height: 100%; }
.rc-card h3 { margin: 0 0 6px; font-size: 1.2rem; }
.rc-card p { color: #bdbdbd; margin: 0; font-size: .95rem; }
.rc-step { font-family:'JetBrains Mono',monospace; color: var(--muted); font-size:.8rem; }
.rc-big { font-family: Oxanium, sans-serif; font-size: 1.35rem; font-weight: 600; }
.rc-up { color: var(--up); } .rc-down { color: var(--down); } .rc-muted { color: var(--muted); }
.rc-callout { border-left: 2px solid rgba(240,240,240,.6); padding: 6px 0 6px 14px; color: #e5e5e5; margin: 8px 0; }
.rc-post { border:1px dashed var(--line); padding: 10px 12px; margin-bottom: 8px; }
.rc-post .who { font-family:'JetBrains Mono',monospace; font-size:.82rem; color: var(--muted); }
a { color: var(--text) !important; }
/* multiselect chips and selected segments: white-on-white with a white primary color */
[data-testid="stMultiSelectTagsContainer"] > span > span { border-radius: 0 !important; border: 1px solid #4a4a4a; }
[data-testid="stMultiSelectTagsContainer"] span { font-family: 'JetBrains Mono', monospace; font-size: .8rem; }
/* selected segment: inverted, like the site's white buttons */
button[aria-checked="true"] { background: var(--text) !important; color: var(--bg) !important; border-color: var(--text) !important; }
button[aria-checked="true"] p { color: var(--bg) !important; }
button[aria-checked] { border-radius: 0 !important; font-family: 'JetBrains Mono', monospace; }
</style>
"""


def header(title: str, subtitle: str, how: str | None = None) -> None:
    st.title(title)
    st.markdown(f'<p style="color:#d4d4d4;font-size:1.08rem;margin-top:-6px">{subtitle}</p>', unsafe_allow_html=True)
    if how:
        with st.expander("How to read this page"):
            st.markdown(how)


def tag(text: str, on: bool = False) -> str:
    return f'<span class="rc-tag{" on" if on else ""}">{text}</span>'


# ---- plain-language formatting --------------------------------------------------
def et(t) -> str:
    """'Sat Oct 24, 3:31 PM ET' from a naive-UTC timestamp."""
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return "—"
    ts = pd.Timestamp(t).to_pydatetime().replace(tzinfo=timezone.utc).astimezone(ET)
    return ts.strftime("%a %b %-d, %-I:%M %p ET")


def until_kickoff(t, kickoff) -> str:
    if t is None or kickoff is None or pd.isna(t) or pd.isna(kickoff):
        return ""
    hrs = (pd.Timestamp(kickoff) - pd.Timestamp(t)).total_seconds() / 3600
    if hrs < 0:
        return "during the game"
    if hrs < 1.5:
        return f"{hrs * 60:.0f} min before kickoff"
    if hrs < 48:
        return f"{hrs:.0f} hours before kickoff"
    return f"{hrs / 24:.0f} days before kickoff"


def ago(t) -> str:
    if t is None or pd.isna(t):
        return "never"
    mins = (datetime.now(timezone.utc).replace(tzinfo=None) - pd.Timestamp(t).to_pydatetime()).total_seconds() / 60
    if mins < 1.5:
        return "just now"
    if mins < 90:
        return f"{mins:.0f} min ago"
    if mins < 48 * 60:
        return f"{mins / 60:.0f} hours ago"
    return f"{mins / 1440:.0f} days ago"


def nick(code: str) -> str:
    return teams.TEAMS.get(code, (code, code))[1]


def size_word(magnitude) -> str:
    if magnitude is None or pd.isna(magnitude):
        return ""
    return "huge" if magnitude >= 2.5 else "big" if magnitude >= 1.6 else "notable"


def move_sentence(m) -> tuple[str, str]:
    """(headline, css class) in plain words, e.g. 'Ravens' chances vs the Bengals fell from 47.5% to 42.5%'."""
    home, away = m["home"], m["away"]
    if m["market_type"] == "winner":
        b, a = m["prob_before"], m["prob_after"]
        verb = "rose" if a > b else "fell"
        return (f"{nick(home)}' chances to beat the {nick(away)} {verb} from {b:.0%} to {a:.0%}",
                "rc-up" if a > b else "rc-down")
    if m["market_type"] == "spread":
        b, a = m["line_before"], m["line_after"]
        half = lambda v: round(abs(v) * 2) / 2          # show spreads like a sportsbook: nearest half point
        fav = lambda v: (f"{nick(home)} favored by {half(v):g}" if half(v) and v < 0
                         else f"{nick(away)} favored by {half(v):g}" if half(v) else "a pick'em")
        key = f", crossing the key number {int(m['crossed_key_number'])}" if pd.notna(m.get("crossed_key_number")) else ""
        return f"Spread moved from {fav(b)} to {fav(a)}{key}", "rc-up" if a < b else "rc-down"
    b, a = round(m["line_before"] * 2) / 2, round(m["line_after"] * 2) / 2
    return (f"Expected total points {'rose' if a > b else 'fell'} from {b:g} to {a:g} ({nick(away)} at {nick(home)})",
            "rc-up" if a > b else "rc-down")


def cause_status(m) -> str:
    """'found', 'none' (no post before the move) or 'unclear' (posts didn't explain it), for display.
    Older rows marked explained whose text says the cause is unclear count as unclear."""
    from pipelines.explain_moves import UNCLEAR
    st_ = m.get("explain_status")
    if st_ == "explained":
        return "unclear" if UNCLEAR.search(m.get("explanation") or "") else "found"
    return {"unexplained": "none", "unclear": "unclear", "refused": "unclear"}.get(st_, "pending")


VENUE_NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket", "pinnacle": "Pinnacle", "consensus": "sportsbooks"}


def venues_text(v: str | None) -> str:
    names = [VENUE_NAMES.get(x, x) for x in (v or "").split(",") if x]
    return " and ".join(names) if names else "—"


def chart_theme(chart: alt.Chart) -> alt.Chart:
    return (chart.configure(background="transparent")
            .configure_view(stroke=None)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor="#262626", domainColor="#3a3a3a",
                            labelFont="JetBrains Mono", titleFont="JetBrains Mono", labelFontSize=11)
            .configure_legend(labelColor="#d4d4d4", titleColor=MUTED, labelFont="JetBrains Mono", orient="bottom")
            .configure_title(color="#f5f5f5", font="Oxanium"))


GLOSSARY = """
**Win probability.** The market's chance that a team wins, from 0% to 100%. On Kalshi or Polymarket a price of 62¢ means 62%.

**Spread.** How many points the favorite is expected to win by. "Bills favored by 3" means the market thinks Buffalo wins by about 3.

**Total.** How many points both teams are expected to score combined.

**Line move.** A sudden change in the market: win probability jumps 3+ points within 30 minutes, the spread crosses an important number (3, 7, 10 or 14), or the total moves 1.5+ points.

**Confirmed on 2 markets.** Kalshi and Polymarket (or the sportsbooks) moved the same way at the same time. That's a much stronger sign real news hit.

**Insider claim.** A checkable statement a reporter posted, like "Lamar Jackson is out Sunday". After the game the app checks it against official data.

**Trust score.** A 0–100 summary of how often an insider is right, how often they're first, and how much the market reacts to them. Everyone starts at 50, and the score moves as their claims get checked.
"""
