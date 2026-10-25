"""Explain each searched move from the insider posts linked to it (Feature A).

For each move with explain_status "searched":
  1. Score every linked post (MOVE_POSTS):
       relevance   = embedding similarity to the move's summary_text, rescaled from the
                     backend's typical range (Snowflake arctic-embed 0.65-0.90, local
                     mpnet 0.15-0.75) to [0, 1], plus TEAM_BONUS when the post
                     names either team or a player on either roster (capped at 1)
       timing      = timing_weight(lead_minutes): 1.0 for posts 0-20 min before the
                     move, falling linearly to 0 at 60 min before, and 0.3 x e^(lead/10)
                     for posts after it (they report on the move rather than cause it)
       attribution = relevance x timing x credibility_weight(author)
     credibility_weight comes from INSIDER_SCORES (milestone 6); 0.5 until scored.
  2. No linked post before the move -> "Unexplained: no insider news before this move"
     (status unexplained; these may be sharp money or news from outside the list).
  3. Otherwise build a numbered evidence block ([M1] the move with numbers from SQL,
     [P1]... the top posts) and ask AI_COMPLETE (temperature 0) for at most three
     sentences, then run rag.guard: unsupported numbers and causal claims without a
     post citation are dropped, unknown citations stripped, and it refuses rather than
     invents (status refused).

Run:  python -m pipelines.explain_moves --hours 24
"""
from __future__ import annotations

import argparse
import math
import re
from datetime import datetime, timedelta

from common import teams
from common.log import get_logger
from pipelines.search_x import utcnow
from rag.guard import guard

log = get_logger("explain_moves")

SIM_FLOOR, SIM_CEILING = 0.65, 0.90
TEAM_BONUS = 0.15
DEFAULT_CREDIBILITY = 0.5
MAX_POSTS = 5
UNEXPLAINED = "Unexplained: no insider news before this move."

PROMPT = """You explain why an NFL betting line moved, using only the numbered evidence below.

Rules:
- Write at most three sentences, plain prose, no lists or headings.
- Copy numbers exactly as written in the evidence. Never compute, round, convert or estimate a number.
- Write citations in square brackets right after the claim, exactly like [P1] or [M1][P2]. Never write "as seen in P1".
- Every claim about a cause must cite the post it comes from.
- Posts marked AFTER the move were reporting on it; describe them as reporting, never as the cause.
- If no post plausibly explains the move, say the cause is unclear from tracked insiders.
- Cite the move as [M1] when you state its numbers.

Evidence:
{evidence}

Explanation:"""


def timing_weight(lead_minutes: float) -> float:
    if lead_minutes < 0:
        return 0.3 * math.exp(lead_minutes / 10)
    if lead_minutes <= 20:
        return 1.0
    return max(0.0, 1 - (lead_minutes - 20) / 40)


def relevance(similarity: float, names_team_or_player: bool,
              sim_range: tuple[float, float] = (SIM_FLOOR, SIM_CEILING)) -> float:
    lo, hi = sim_range
    base = min(1.0, max(0.0, (similarity - lo) / (hi - lo)))
    return round(min(1.0, base + (TEAM_BONUS if names_team_or_player else 0.0)), 4)


def attribution(rel: float, lead_minutes: float, credibility: float) -> float:
    return round(rel * timing_weight(lead_minutes) * credibility, 4)


def mentions(text: str, game: dict | None, player_names: list[str]) -> bool:
    if not text or not game:
        return False
    low = text.lower()
    words = []
    for code in (game["home"], game["away"]):
        full, nick = teams.TEAMS[code]
        words += [nick.lower(), full.rsplit(" ", 1)[0].lower()]
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
        return True
    return any(n and n.lower() in low for n in player_names)


def evidence_block(move: dict, posts: list[dict]) -> tuple[str, set[str], set[str]]:
    """Numbered evidence. Returns (text, valid ids, ids of posts after the move)."""
    lines = [f"[M1] {move['summary_text']}"]
    after = set()
    for i, p in enumerate(posts, 1):
        pid = f"P{i}"
        lead = p["lead_minutes"]
        when = (f"posted {abs(lead):g} minutes before the move started" if lead >= 0
                else f"posted {abs(lead):g} minutes AFTER the move started")
        if lead < 0:
            after.add(pid)
        text = " ".join((p["text"] or "(text unavailable)").split())
        lines.append(f"[{pid}] @{p['author']}, {when} ({p['posted_at']:%H:%M} UTC): \"{text}\"")
    return "\n".join(lines), {"M1"} | {f"P{i}" for i in range(1, len(posts) + 1)}, after


def credibility(store, author: str) -> float:
    w = store.insider_credibility(author)
    return DEFAULT_CREDIBILITY if w is None else w


def explain(store, ai, move: dict, game: dict | None, player_names: list[str]) -> dict:
    """Score the move's posts and write its explanation. Returns the LINE_MOVES update."""
    links = store.move_posts(move["move_id"])
    before = [l for l in links if l["lead_minutes"] >= 0]
    if not before:
        return {"move_id": move["move_id"], "explain_status": "unexplained", "explanation": UNEXPLAINED}
    sims = ai.similarities(move["summary_text"], [l["text"] or "" for l in links])
    scored = []
    for l, sim in zip(links, sims):
        rel = relevance(sim, mentions(l["text"], game, player_names),
                        getattr(ai, "sim_range", (SIM_FLOOR, SIM_CEILING)))
        att = attribution(rel, l["lead_minutes"], credibility(store, l["author"]))
        scored.append({**l, "relevance": rel, "attribution_score": att})
    store.upsert_move_posts([{"move_id": move["move_id"], "post_id": s["post_id"],
                              "relevance": s["relevance"], "attribution_score": s["attribution_score"]}
                             for s in scored])
    top = sorted(scored, key=lambda s: -s["attribution_score"])[:MAX_POSTS]
    top.sort(key=lambda s: s["posted_at"])
    evidence, valid, after = evidence_block(move, top)
    answer = ai.complete(PROMPT.format(evidence=evidence), temperature=0)
    res = guard(answer.strip().strip('"'), evidence, valid, after)
    for sentence, why in res.dropped:
        log.info("guard dropped (%s): %s", why, sentence[:120])
    return {"move_id": move["move_id"], "explanation": res.text,
            "explain_status": "refused" if res.refused else "explained"}


def run(store, ai, hours: float = 24, now: datetime | None = None) -> int:
    now = now or utcnow()
    moves = store.line_moves(now - timedelta(hours=hours), now, explain_status="searched")
    if not moves:
        return 0
    games = {g["game_id"]: g for g in store.games_between(now - timedelta(days=3), now + timedelta(days=10))}
    updates = []
    for m in moves:
        g = games.get(m["game_id"])
        names = []
        if g:
            for t in (g["home"], g["away"]):
                names += [r["full_name"] for r in store.roster(g["season"], t)]
        try:
            updates.append(explain(store, ai, m, g, sorted(set(names))))
        except Exception as e:  # one bad move must not block the rest
            log.error("explain failed for %s: %s", m["move_id"], e)
    store.upsert_line_moves(updates)
    log.info("explained %d moves: %s", len(updates),
             {s: sum(u["explain_status"] == s for u in updates) for s in ("explained", "unexplained", "refused")})
    return len(updates)


def ai_backend() -> str:
    """AI_BACKEND=snowflake | local; by default Snowflake when an account is configured."""
    from common.settings import env
    choice = (env("AI_BACKEND") or ("snowflake" if env("SNOWFLAKE_ACCOUNT") else "local")).lower()
    return choice if choice in ("snowflake", "local") else "snowflake"


def get_ai():
    """Snowflake Cortex or the local stand-ins (sentence-transformers + Ollama).
    None when the chosen backend isn't reachable; AI steps then wait."""
    from common.settings import env
    if ai_backend() == "local":
        from common.local_ai import LocalAI
        ai = LocalAI()
        if not ai.available():
            log.warning("local AI unavailable: start `ollama serve` and `ollama pull %s`", ai.llm_model)
            return None
        return ai
    if not env("SNOWFLAKE_ACCOUNT"):
        return None
    from store.snowflake import SnowflakeStore
    ai = SnowflakeStore()
    ai._execute(f"USE WAREHOUSE {env('SNOWFLAKE_WAREHOUSE')}")
    return ai


def main() -> None:
    from store import get_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args()
    store = get_store()
    store.create_schema()
    ai = get_ai()
    if ai is None:
        raise SystemExit("explanations need Snowflake Cortex: set SNOWFLAKE_ACCOUNT in .env")
    try:
        run(store, ai, args.hours)
    finally:
        ai.suspend_warehouse()
        ai.close()


if __name__ == "__main__":
    main()
