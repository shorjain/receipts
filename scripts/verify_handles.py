"""Confirm each unverified handle in config/insiders.yaml, and find the right handle
for ones that don't check out.

verify (default): for each batch of unverified handles, x_search looks for posts from
  the last --days days. A handle is verified when a cited post's oEmbed author is that
  handle AND the account's display name matches the configured name. A search returns
  roughly 60 posts, so rerun leftovers with a smaller --batch.
--find: for each still-unverified entry, ask x_search for "<name>, <outlet>, <team>"
  across all of X, then accept a handle only if oEmbed confirms a recent post whose
  display name matches the whole configured name and, for beat reporters, whose posts
  mention their team (so a same-named stranger doesn't pass). The YAML handle is replaced and marked
  verified; the old handle is kept in a comment.

Model-written text is never trusted for handles: only cited post IDs and oEmbed.
Costs one search per batch (verify) or per entry (--find), logged in SEARCH_RUNS as
purpose "verify"; setup searches don't count against XAI_DAILY_BUDGET_USD.

Run:  python -m scripts.verify_handles [--days 14] [--batch 4] [--find] [--dry-run]
"""
import argparse
import re
from datetime import timedelta

import httpx
from rapidfuzz import fuzz

from common import teams
from common.settings import ROOT, insiders
from pipelines.search_x import run_search, utcnow
from sources import xsearch
from sources.xsearch import XSearchClient, batch_handles
from store import get_store

YAML = ROOT / "config" / "insiders.yaml"
NAME_MATCH = 90


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def names_match(display: str | None, configured: str) -> bool:
    """The whole configured name must match: 'Brooke Pryor' or 'MikeKlis9NEWS' pass for
    Brooke Pryor / Mike Klis; 'David Teicher' or plain 'Adam' don't pass for Adam Teicher."""
    if not display:
        return False
    return (fuzz.token_sort_ratio(display.lower(), configured.lower()) >= NAME_MATCH
            or _compact(configured) in _compact(display))


def mentions_team(texts: list[str], team_codes: list[str]) -> bool:
    """True when any post mentions one of the teams by nickname or city."""
    words = {w.lower() for code in team_codes for w in (teams.TEAMS[code][1], teams.TEAMS[code][0].rsplit(" ", 1)[0])}
    return any(w in (t or "").lower() for t in texts for w in words)


def mark_verified(text: str, handle: str) -> str:
    """Flip `verified: false` to true inside one handle's block of the YAML."""
    pat = re.compile(rf"(- handle: {re.escape(handle)}\n(?:  .*\n)*?  verified: )false")
    return pat.sub(r"\1true", text, count=1)


def replace_handle(text: str, old: str, new: str) -> str:
    """Swap one entry's handle (leaving a comment with the old one) and mark it verified."""
    text = text.replace(f"- handle: {old}\n", f"# handle corrected from {old}\n- handle: {new}\n", 1)
    return mark_verified(text, new)


def verify(store, client, todo: list[dict], days: int, batch: int | None) -> tuple[set[str], dict]:
    by_handle = {r["handle"]: r for r in todo}
    found, mismatched, http = set(), {}, httpx.Client(timeout=15, follow_redirects=True)
    now = utcnow()
    for b in batch_handles([r["handle"] for r in todo], batch):
        posts = run_search(store, client, b, now - timedelta(days=days), now, "verify", now=now)
        for p in posts:
            h = p["author"]
            if h in found or h in mismatched:
                continue
            display = xsearch.author_name(xsearch.fetch_oembed(p["post_id"], http))
            if names_match(display, by_handle[h]["name"]):
                found.add(h)
            else:
                mismatched[h] = display
    return found, mismatched


def find(store, client, entries: list[dict], days: int) -> dict[str, str]:
    """old handle -> confirmed new handle."""
    fixes, http, now = {}, httpx.Client(timeout=15, follow_redirects=True), utcnow()
    for r in entries:
        team = ", ".join(r.get("teams", []))
        prompt = (f"Find the X account of {r['name']}, the {r['outlet']} NFL reporter"
                  f"{' covering ' + team if team else ''}. Search for their recent posts and give "
                  f"the URLs of two of their posts from the last {days} days.")
        res = client.search(None, now - timedelta(days=days), now, prompt=prompt)
        store.insert_search_run({"run_id": f"find-{r['handle']}-{now:%H%M%S}", "purpose": "verify",
                                 "handles": r["handle"], "citations": res.citations, "error": res.error,
                                 "cost_estimate_usd": res.cost_estimate_usd, "ran_at": now})
        # Group cited posts by oEmbed author; accept the author whose display name matches
        # and (for beat reporters) who mentions their team. Same-name strangers fail here.
        seen: dict[str, dict] = {}
        for url in res.citations:
            pid = xsearch.post_id_from_url(url)
            embed = xsearch.fetch_oembed(pid, http) if pid else None
            if not embed:
                continue
            author, text = xsearch.parse_oembed(embed)
            d = seen.setdefault(author, {"display": xsearch.author_name(embed), "texts": []})
            d["texts"].append(text)
        for author, d in seen.items():
            if names_match(d["display"], r["name"]) and (not r.get("teams") or mentions_team(d["texts"], r["teams"])):
                fixes[r["handle"]] = author
                break
        print(f"  {r['name']:<22} {r['handle']:<18} -> {fixes.get(r['handle'], 'not found')}  (~${res.cost_estimate_usd:.3f})")
    return fixes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--batch", type=int, default=None, help="handles per verify search (default 10)")
    ap.add_argument("--find", action="store_true", help="look up correct handles for unverified entries")
    ap.add_argument("--dry-run", action="store_true", help="report only; don't edit the YAML")
    args = ap.parse_args()
    todo = [r for r in insiders() if not r.get("verified")]
    if not todo:
        print("all handles already verified")
        return
    store, client = get_store(), XSearchClient()
    store.create_schema()
    text = YAML.read_text()
    if args.find:
        fixes = find(store, client, todo, args.days)
        for old, new in fixes.items():
            text = replace_handle(text, old, new)
        missing = [r["handle"] for r in todo if r["handle"] not in fixes]
        print(f"corrected {len(fixes)}/{len(todo)}; still unknown: {', '.join(missing) or 'none'}")
    else:
        found, mismatched = verify(store, client, todo, args.days, args.batch)
        for h in sorted(found):
            text = mark_verified(text, h)
        print(f"verified {len(found)}/{len(todo)}: {', '.join(sorted(found))}")
        for h, display in mismatched.items():
            print(f"  name mismatch: @{h} is '{display}', not the configured name")
        missing = [r["handle"] for r in todo if r["handle"] not in found and r["handle"] not in mismatched]
        print(f"no posts in {args.days} days (try --find): {', '.join(missing) or 'none'}")
    if not args.dry_run:
        YAML.write_text(text)


if __name__ == "__main__":
    main()
