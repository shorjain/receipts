"""Extract checkable claims from insider posts (Feature B, step 1).

For each batch of posts not yet read, AI_COMPLETE with structured output returns the
factual claims in them, matching the CLAIMS schema. Then:
  - player names are fuzzy-matched to nflverse player IDs (name + team); a name that
    doesn't match clearly is stored with player_id null rather than guessed;
  - each claim gets the game it's about and resolve_by (when it can be checked);
  - claims for the same player, type and value within STORY_WINDOW are one story. The
    earliest post that doesn't credit someone else gets the scoop (is_original true);
    later posts and posts that credit someone ("per @X") get is_original false.

Run:  python -m pipelines.extract_claims [--limit 500]
"""
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timedelta

from rapidfuzz import fuzz, process

from common import teams
from common.log import get_logger
from pipelines.search_x import utcnow

log = get_logger("extract_claims")

BATCH = 15
STORY_WINDOW = timedelta(hours=6)
CLAIM_TYPES = ["active", "inactive", "questionable_will_play", "starter", "trade", "signing",
               "release", "return_date", "other"]
GAME_TYPES = {"active", "inactive", "questionable_will_play", "starter"}

SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {"type": "object", "properties": {
        "post": {"type": "integer", "description": "the [n] number of the post"},
        "player": {"type": "string", "description": "full player name as written, or '' if none"},
        "team": {"type": "string", "description": "team abbreviation the claim is about, or ''"},
        "claim_type": {"type": "string", "enum": CLAIM_TYPES},
        "predicted_value": {"type": "string", "description": "short value: 'out', 'will play', "
                            "'starting QB', 'to NYJ', 'week 9', ..."},
        "hedging": {"type": "string", "enum": ["confirmed", "expected", "rumor"]},
        "credits_other": {"type": "boolean", "description": "true if the post credits another "
                          "source (per @X, via, h/t, reports say)"},
        "credited_handle": {"type": "string", "description": "the credited X handle without @, or ''"},
        "claim_text": {"type": "string", "description": "the exact words from the post that make the claim"},
    }, "required": ["post", "player", "team", "claim_type", "predicted_value", "hedging",
                    "credits_other", "credited_handle", "claim_text"]}}},
    "required": ["claims"],
}

PROMPT = """You extract checkable factual claims about NFL players from posts by NFL reporters.

A claim is a statement that can later be checked against official data:
- inactive: the player WON'T play in the team's next game: "out", "ruled out", "inactive",
  "won't play", "not expected to play", "will miss", "doubtful", "suspended" (predicted_value "out")
- active: the player WILL play: "will play", "active", "expected to play", "on track to play",
  "cleared", "will make his debut" (predicted_value "will play")
- questionable_will_play: a player listed questionable is expected to play
- starter: the player will start (e.g. "X will start at QB")
- trade / signing / release: a roster move (predicted_value names the new team for trades/signings, e.g. "to NYJ")
- return_date: when an injured player will return (predicted_value like "week 9")
- other: any other checkable claim about a specific player
Skip opinions, analysis, schedules, links, recaps of finished games, and posts with no claim.
hedging: "confirmed" for definite statements, "expected" for "expected to", "likely", "plans to",
"rumor" for "could", "considering", "there's buzz", "per sources it's possible".
Team abbreviations: {teams}.
Return every claim; one post can have several, most have none.

Posts:
{posts}"""


def claim_id(post_id: str, i: int) -> str:
    return hashlib.sha1(f"{post_id}|{i}".encode()).hexdigest()[:16]


def norm_team(t: str | None) -> str | None:
    if not t:
        return None
    try:
        return teams.canon(t)
    except KeyError:
        try:
            return teams.from_full_name(t)
        except KeyError:
            nick = {v[1].lower(): k for k, v in teams.TEAMS.items()}
            return nick.get(t.lower().strip())


def roster_index(rows: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    """team -> [(full_name, player_id, team)] from the latest week each player appears."""
    latest: dict[str, dict] = {}
    for r in rows:
        if r["player_id"] not in latest or r["week"] > latest[r["player_id"]]["week"]:
            latest[r["player_id"]] = r
    idx: dict[str, list] = {}
    for r in latest.values():
        idx.setdefault(r["team"], []).append((r["full_name"], r["player_id"], r["team"]))
        idx.setdefault("*", []).append((r["full_name"], r["player_id"], r["team"]))
    return idx


def _best(name: str, pool: list, threshold: int, unique: bool):
    if not pool:
        return None
    hits = process.extract(name, [p[0] for p in pool], scorer=fuzz.token_sort_ratio, limit=2)
    if not hits or hits[0][1] < threshold:
        return None
    if unique and len(hits) > 1 and hits[1][1] >= threshold and pool[hits[1][2]][1] != pool[hits[0][2]][1]:
        return None  # two different players share the name; don't guess
    return pool[hits[0][2]]


def match_player(name: str, team: str | None, idx: dict) -> tuple[str | None, str | None]:
    """(player_id, team) when the name matches clearly, else (None, team).

    First within the stated team (score >= 90). Failing that, across all rosters with a
    near-exact unique match (>= 95), taking the roster's team: reporters and the model
    sometimes name a player's old team after a trade."""
    if not name:
        return None, team
    hit = _best(name, idx.get(team, []), 90, unique=False) if team else None
    if hit is None:
        hit = _best(name, idx.get("*", []), 95, unique=True)
    if hit is None:
        return None, team
    return hit[1], hit[2]


def next_game(team: str | None, after: datetime, games: list[dict]) -> dict | None:
    if not team:
        return None
    upcoming = [g for g in games if team in (g["home"], g["away"]) and g["kickoff_at"] >= after - timedelta(hours=3)]
    return min(upcoming, key=lambda g: g["kickoff_at"]) if upcoming else None


def resolve_target(claim_type: str, team: str | None, posted_at: datetime, value: str,
                   games: list[dict]) -> tuple[str | None, datetime | None]:
    """(game_id, resolve_by) for a claim."""
    if claim_type in GAME_TYPES:
        g = next_game(team, posted_at, games)
        return (g["game_id"], g["kickoff_at"] + timedelta(hours=4)) if g else (None, None)
    if claim_type in ("trade", "signing", "release"):
        return None, posted_at + timedelta(days=7)
    if claim_type == "return_date":
        m = re.search(r"week\s*(\d+)", value or "", re.I)
        if m and team:
            week = int(m.group(1))
            g = next((g for g in games if team in (g["home"], g["away"]) and g["week"] == week), None)
            if g:
                return g["game_id"], g["kickoff_at"] + timedelta(hours=4)
        return None, posted_at + timedelta(days=42)
    return None, None


def story_key(c: dict) -> tuple:
    who = c.get("player_id") or (c.get("player_name") or "").lower().strip()
    value = re.sub(r"[^a-z0-9 ]", "", (c.get("predicted_value") or "").lower()).strip()
    return who, c["claim_type"], value


def assign_stories(claims: list[dict]) -> list[dict]:
    """Group claims into stories and set story_id / is_original. Returns updates."""
    groups: dict[tuple, list[dict]] = {}
    for c in sorted(claims, key=lambda c: c["posted_at"]):
        if not story_key(c)[0]:
            continue
        groups.setdefault(story_key(c), []).append(c)
    updates = []
    for key, cs in groups.items():
        story, first = None, None
        scooped = False
        for c in cs:
            if story is None or c["posted_at"] - first > STORY_WINDOW:
                first, scooped = c["posted_at"], False
                story = hashlib.sha1(f"{key}|{first:%Y-%m-%dT%H:%M}".encode()).hexdigest()[:16]
            original = not scooped and not c.get("credited_handle")
            scooped = scooped or original
            updates.append({"claim_id": c["claim_id"], "story_id": story, "is_original": original})
    return updates


def format_posts(posts: list[dict]) -> str:
    return "\n".join(f"[{i}] @{p['author']} ({p['posted_at']:%Y-%m-%d %H:%M} UTC): {' '.join((p['text'] or '').split())}"
                     for i, p in enumerate(posts, 1))


def extract_batch(ai, posts: list[dict], idx: dict, games: list[dict]) -> list[dict]:
    prompt = PROMPT.replace("{teams}", ", ".join(sorted(teams.TEAMS))).replace("{posts}", format_posts(posts))
    out = ai.complete_json(prompt, SCHEMA) or {}
    rows, counter = [], {}
    for c in out.get("claims") or []:
        n = c.get("post")
        if not isinstance(n, int) or not 1 <= n <= len(posts) or c.get("claim_type") not in CLAIM_TYPES:
            continue
        p = posts[n - 1]
        team = norm_team(c.get("team"))
        pid, team = match_player(c.get("player", ""), team, idx)
        game_id, resolve_by = resolve_target(c["claim_type"], team, p["posted_at"], c.get("predicted_value", ""), games)
        i = counter[p["post_id"]] = counter.get(p["post_id"], -1) + 1
        rows.append({
            "claim_id": claim_id(p["post_id"], i), "post_id": p["post_id"], "author": p["author"],
            "posted_at": p["posted_at"], "player_id": pid, "player_name": c.get("player") or None,
            "team": team, "claim_type": c["claim_type"], "predicted_value": c.get("predicted_value") or None,
            "hedging": c.get("hedging") if c.get("hedging") in ("confirmed", "expected", "rumor") else "confirmed",
            "credited_handle": (c.get("credited_handle") or "").lstrip("@") or ("unknown" if c.get("credits_other") else None),
            "game_id": game_id, "resolve_by": resolve_by, "claim_text": c.get("claim_text") or None,
        })
    return rows


def run(store, ai, limit: int = 500, now: datetime | None = None) -> int:
    now = now or utcnow()
    posts = store.posts_needing_claims(limit)
    if not posts:
        return 0
    season = max(g["season"] for g in store.get_games()) if store.get_games() else now.year
    idx = roster_index(store.roster(season))
    games = store.games_between(now - timedelta(days=30), now + timedelta(days=60))
    total = 0
    for i in range(0, len(posts), BATCH):
        batch = posts[i:i + BATCH]
        try:
            rows = extract_batch(ai, batch, idx, games)
        except Exception as e:  # leave the batch unchecked; it's retried next run
            log.error("extraction failed for %d posts: %s", len(batch), e)
            continue
        store.upsert_claims(rows)
        store.mark_claims_checked([p["post_id"] for p in batch], now)
        total += len(rows)
    store.upsert_claims(assign_stories(store.claims(since=now - timedelta(days=21))))
    log.info("extracted %d claims from %d posts", total, len(posts))
    return total


def main() -> None:
    from pipelines.explain_moves import get_ai
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    ai = get_ai()
    if ai is None:
        raise SystemExit("claim extraction needs Snowflake Cortex: set SNOWFLAKE_ACCOUNT in .env")
    try:
        run(store, ai, args.limit)
    finally:
        ai.suspend_warehouse()
        ai.close()


if __name__ == "__main__":
    main()
