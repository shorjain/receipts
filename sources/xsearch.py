"""Read tracked insiders' X posts through Grok's server-side x_search tool.

Trust rules (from the brief, tightened):
  - A post is kept only if its URL is in the response's server-side citations.
  - posted_at comes from the post ID (X snowflake: (id >> 22) + 1288834974657 ms).
  - author and text come from X's public oEmbed endpoint for that post ID, never from
    model-written text. A post whose author isn't one of the searched handles is
    dropped. text_source is "oembed" (verified text) or "citation_only" (no text).

API quirks found while probing (Oct 2026), handled here:
  - x_search's to_date filters by whole days: to_date=now drops today's posts. We pass
    only from_date and filter to the exact window with ID timestamps.
  - Prompts must name each handle and ask to search each one; vaguer prompts made the
    model run searches that matched nothing.
  - oEmbed moved to publish.x.com.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

from common.settings import env, env_float, env_int

TWITTER_EPOCH_MS = 1288834974657
OEMBED_URL = "https://publish.x.com/oembed"
_STATUS = re.compile(r"(?:x|twitter)\.com/(?:i(?:/web)?|[A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")


# ---- IDs and timestamps -----------------------------------------------------------
def post_id_from_url(url: str) -> str | None:
    m = _STATUS.search(url or "")
    return m.group(1) if m else None


def posted_at_from_id(post_id: str | int) -> datetime:
    """Naive UTC datetime encoded in an X post ID."""
    ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)


# ---- oEmbed -------------------------------------------------------------------------
class _FirstParagraph(HTMLParser):
    """Collects the text of the first <p> in an oEmbed blockquote (the post body)."""

    def __init__(self):
        super().__init__()
        self.depth, self.done, self.parts = 0, False, []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "p":
            self.depth += 1
        elif tag == "br" and self.depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "p" and self.depth:
            self.depth -= 1
            self.done = self.depth == 0

    def handle_data(self, data):
        if self.depth and not self.done:
            self.parts.append(data)


def author_name(embed: dict | None) -> str | None:
    return (embed or {}).get("author_name")


def parse_oembed(embed: dict) -> tuple[str | None, str | None]:
    """(author handle, post text) from an oEmbed payload."""
    author = (embed.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1] or None
    p = _FirstParagraph()
    p.feed(embed.get("html") or "")
    text = html.unescape("".join(p.parts)).strip() or None
    return author, text


def fetch_oembed(post_id: str, http: httpx.Client | None = None, retries: int = 3) -> dict | None:
    """oEmbed payload for a post, or None if it's deleted, protected or unreachable."""
    http = http or httpx.Client(timeout=15, follow_redirects=True)
    for attempt in range(retries + 1):
        try:
            r = http.get(OEMBED_URL, params={"url": f"https://x.com/i/status/{post_id}",
                                             "omit_script": "true", "dnt": "true"})
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            return r.json()
        if r is not None and r.status_code in (403, 404):
            return None
        time.sleep(min(10.0, 0.5 * 2 ** attempt))
    return None


# ---- search -------------------------------------------------------------------------
@dataclass
class SearchResult:
    content: str = ""
    citations: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    tool_calls: int = 0
    cost_estimate_usd: float = 0.0
    error: str | None = None


def estimate_cost(usage: dict, tool_calls: int) -> float:
    """USD estimate from token usage and search count. Rates are env-tunable; check
    the Logs page on console.x.ai for exact charges."""
    return round(usage.get("prompt_tokens", 0) / 1e6 * env_float("XAI_PRICE_IN_PER_M", 1.25)
                 + usage.get("completion_tokens", 0) / 1e6 * env_float("XAI_PRICE_OUT_PER_M", 2.5)
                 + tool_calls * env_float("XAI_SEARCH_FEE_USD", 0.005), 5)


def build_prompt(handles: list[str], start: datetime, end: datetime, focus: str | None = None) -> str:
    names = ", ".join("@" + h for h in handles)
    ask = (f"List every post from these NFL accounts between {start:%Y-%m-%d %H:%M} UTC and "
           f"{end:%Y-%m-%d %H:%M} UTC: {names}. Search each account for its posts in that "
           f"time range, including replies and reposts with comments, and give each post's URL.")
    if focus:
        ask += f" Pay particular attention to news about: {focus}."
    return ask


def batch_handles(handles: list[str], size: int | None = None) -> list[list[str]]:
    size = size or env_int("XAI_MAX_HANDLES_PER_SEARCH", 10)
    return [handles[i:i + size] for i in range(0, len(handles), size)]


class XSearchClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from xai_sdk import Client  # lazy: tests never import the network client
        self.client = Client(api_key=api_key or env("XAI_API_KEY"), timeout=env_float("XAI_TIMEOUT_SECONDS", 120))
        self.model = model or env("XAI_MODEL", "grok-4.20-non-reasoning")

    def search(self, handles: list[str] | None, start: datetime, end: datetime,
               focus: str | None = None, prompt: str | None = None) -> SearchResult:
        """Search posts by `handles` (or all of X when None) since `start`."""
        from xai_sdk.chat import user
        from xai_sdk.tools import x_search
        # to_date is day-granular (see module docstring): leave it off for windows ending
        # today; for past windows, the next day bounds the search without losing posts.
        today = datetime.now(timezone.utc).date()
        to_date = (end + timedelta(days=1)).replace(tzinfo=timezone.utc) if end.date() < today else None
        try:
            chat = self.client.chat.create(
                model=self.model, temperature=0,
                tools=[x_search(from_date=start.replace(tzinfo=timezone.utc) - timedelta(minutes=5),
                                to_date=to_date, allowed_x_handles=handles or None)])
            chat.append(user(prompt or build_prompt(handles or [], start, end, focus)))
            r = chat.sample()
        except Exception as e:  # noqa: BLE001 - logged in SEARCH_RUNS, never raised into the loop
            return SearchResult(error=f"{type(e).__name__}: {e}"[:500])
        usage = {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens,
                 "cached_prompt_tokens": getattr(r.usage, "cached_prompt_text_tokens", 0)}
        calls = sum(dict(r.server_side_tool_usage).values())
        return SearchResult(content=r.content, citations=list(r.citations), usage=usage,
                            tool_calls=calls, cost_estimate_usd=estimate_cost(usage, calls))


# ---- verification -------------------------------------------------------------------
def verify_posts(citations: list[str], allowed_handles: list[str], oembed=fetch_oembed,
                 known: dict[str, dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Turn cited URLs into POSTS rows. Returns (posts, rejected).

    `known` maps post_id -> existing POSTS row, so posts already stored skip oEmbed.
    """
    canon = {h.lower(): h for h in allowed_handles}
    known = known or {}
    posts, rejected, seen = [], [], set()
    for url in citations:
        pid = post_id_from_url(url)
        if not pid or pid in seen:
            if not pid:
                rejected.append({"url": url, "reason": "not_a_post"})
            continue
        seen.add(pid)
        if pid in known:
            row = known[pid]
            if row["author"].lower() in canon:
                posts.append(row)
            continue
        embed = oembed(pid)
        if embed is None:
            rejected.append({"url": url, "post_id": pid, "reason": "oembed_unavailable"})
            continue
        author, text = parse_oembed(embed)
        if not author or author.lower() not in canon:
            rejected.append({"url": url, "post_id": pid, "author": author, "reason": "author_not_searched"})
            continue
        author = canon[author.lower()]
        posts.append({
            "post_id": pid, "url": f"https://x.com/{author}/status/{pid}", "author": author,
            "posted_at": posted_at_from_id(pid), "text": text,
            "text_source": "oembed" if text else "citation_only",
        })
    return posts, rejected
