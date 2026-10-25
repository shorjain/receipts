from datetime import datetime

import pytest

from sources import xsearch
from tests.conftest import load_fixture

FIX = load_fixture("xsearch_response.json")


@pytest.mark.parametrize("url,pid", [
    ("https://x.com/i/status/2114031855666122795", "2114031855666122795"),
    ("https://x.com/AdamSchefter/status/2114031855666122795?s=20", "2114031855666122795"),
    ("https://twitter.com/RapSheet/statuses/2114098771030057184", "2114098771030057184"),
    ("https://x.com/i/web/status/123", "123"),
    ("https://x.com/i/user/15490418", None),
    ("https://example.com/status/123", None),
])
def test_post_id_from_url(url, pid):
    assert xsearch.post_id_from_url(url) == pid


def test_posted_at_from_id():
    # Schefter's Browns post, verified against X: 2026-10-24 16:30:45.860 UTC
    assert xsearch.posted_at_from_id("2114031855666122795") == datetime(2026, 10, 24, 16, 30, 45, 860000)
    # The ID 20 (2006, pre-snowflake) is not decodable; snowflake IDs always are.
    assert xsearch.posted_at_from_id(1 << 22) == datetime(2010, 11, 4, 1, 42, 54, 658000)


def test_parse_oembed_extracts_author_and_text():
    embed = {"author_url": "https://x.com/AdamSchefter",
             "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">Browns activated RB '
                     'Dylan Sampson &amp; he&#39;s expected to play.<br>More soon. '
                     '<a href="https://t.co/x">pic.twitter.com/x</a></p>&mdash; Adam Schefter '
                     '(@AdamSchefter) <a href="https://x.com/AdamSchefter/status/1">October 24, 2026</a></blockquote>'}
    author, text = xsearch.parse_oembed(embed)
    assert author == "AdamSchefter"
    assert text == "Browns activated RB Dylan Sampson & he's expected to play.\nMore soon. pic.twitter.com/x"


def test_verify_recorded_response_keeps_only_cited_tracked_posts():
    embeds = FIX["oembed"]
    posts, rejected = xsearch.verify_posts(FIX["citations"], FIX["handles"], oembed=embeds.get)
    assert len(posts) == len({xsearch.post_id_from_url(u) for u in FIX["citations"]})
    assert {p["author"] for p in posts} <= set(FIX["handles"])
    for p in posts:
        assert p["posted_at"] == xsearch.posted_at_from_id(p["post_id"])
        assert p["url"] == f"https://x.com/{p['author']}/status/{p['post_id']}"
        assert p["text_source"] in ("oembed", "citation_only")
    assert rejected == []


def test_verify_rejects_untracked_authors_and_non_posts():
    embed = {"author_url": "https://x.com/AlbertBreer", "html": "<blockquote><p>hi</p></blockquote>"}
    posts, rejected = xsearch.verify_posts(
        ["https://x.com/i/status/111", "https://x.com/i/user/5", "https://x.com/i/status/222"],
        ["AdamSchefter"], oembed=lambda pid: embed if pid == "111" else None)
    assert posts == []
    assert [r["reason"] for r in rejected] == ["author_not_searched", "not_a_post", "oembed_unavailable"]


def test_verify_ignores_model_text_and_uses_canonical_handle_case():
    embed = {"author_url": "https://x.com/adamschefter", "html": "<blockquote><p>real text</p></blockquote>"}
    posts, _ = xsearch.verify_posts(["https://x.com/RapSheet/status/2114031855666122795"],
                                    ["AdamSchefter"], oembed=lambda pid: embed)
    # The URL claimed RapSheet; oEmbed says Schefter, and config spelling wins.
    assert posts[0]["author"] == "AdamSchefter" and posts[0]["text"] == "real text"


def test_known_posts_skip_oembed():
    known = {"2114031855666122795": {"post_id": "2114031855666122795", "author": "AdamSchefter"}}
    calls = []
    posts, _ = xsearch.verify_posts(["https://x.com/i/status/2114031855666122795"], ["AdamSchefter"],
                                    oembed=lambda pid: calls.append(pid), known=known)
    assert posts == [known["2114031855666122795"]] and calls == []


def test_cost_estimate_from_recorded_usage(monkeypatch):
    for k, v in {"XAI_PRICE_IN_PER_M": "1.25", "XAI_PRICE_OUT_PER_M": "2.5", "XAI_SEARCH_FEE_USD": "0.005"}.items():
        monkeypatch.setenv(k, v)
    cost = xsearch.estimate_cost(FIX["usage"], FIX["tool_calls"])
    assert cost == pytest.approx(51434 / 1e6 * 1.25 + 4471 / 1e6 * 2.5 + 15 * 0.005)


def test_prompt_names_every_handle_and_window():
    p = xsearch.build_prompt(["AdamSchefter", "RapSheet"], datetime(2026, 10, 25, 9), datetime(2026, 10, 25, 15))
    assert "@AdamSchefter, @RapSheet" in p and "2026-10-25 09:00 UTC" in p and "Search each account" in p


def test_batch_handles():
    assert xsearch.batch_handles(list("abcdefghijkl"), 5) == [list("abcde"), list("fghij"), list("kl")]
