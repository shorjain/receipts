"""Ask retrieval: turn a parsed question into numbered evidence items.

Filters with SQL through store.ask_* (Snowflake also ranks posts and moves by
VECTOR_COSINE_SIMILARITY to the question). Each item is {id, kind, line, url, t} with
kind M (move), P (post), C (claim), S (insider stats) or G (model prediction).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import teams
from rag.parse import Query

ET = ZoneInfo("America/New_York")
DEFAULT_RECENT = timedelta(days=7)


def _et(t: datetime) -> str:
    return t.replace(tzinfo=timezone.utc).astimezone(ET).strftime("%a %b %d %I:%M %p ET").replace(" 0", " ")


def _short(text: str | None, n: int = 260) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _matchup(game: dict | None) -> str:
    return f"{game['away']} at {game['home']}" if game else "unknown game"


def gather(store, q: Query, now: datetime, season_start: datetime) -> list[dict]:
    """Evidence items for a parsed question."""
    from pipelines.score_insiders import author_stats
    start, end = q.start, q.end
    recent_start, recent_end = start or now - DEFAULT_RECENT, end or now
    games = {x["game_id"]: x for x in store.get_games()}
    team_set = set(q.teams) | {p["team"] for p in q.players if p.get("team")}
    game_ids = [gid for gid, x in games.items() if team_set & {x["home"], x["away"]}
                and recent_start - timedelta(days=7) <= x["kickoff_at"] <= recent_end + timedelta(days=10)]
    names = [p["full_name"] for p in q.players]
    terms = names + [teams.TEAMS[t][1] for t in q.teams]
    items: list[dict] = []
    want = {"moves": q.intent in ("moves", "any"), "posts": q.intent in ("moves", "scoop", "any"),
            "claims": q.intent in ("scoop", "scores", "any"), "scores": q.intent in ("scores",) or bool(q.insiders),
            "predictions": q.intent in ("predictions", "any") and bool(team_set)}

    if want["moves"] and (game_ids or not team_set):
        for m in store.ask_moves(recent_start, recent_end, game_ids or None, q.text, limit=5):
            gm = games.get(m["game_id"])
            line = f"{_matchup(gm)}: {m['summary_text']}"
            if m.get("leader_venue") and m.get("lead_lag_minutes") is not None:
                line += f" {m['leader_venue']} moved first by {m['lead_lag_minutes']:g} minutes."
            if m.get("explanation") and m.get("explain_status") in ("explained", "unexplained"):
                line += f" Stored explanation: {m['explanation']}"
            items.append({"kind": "M", "line": line, "url": None, "t": m["window_start"]})
    if want["posts"]:
        posts = store.ask_posts(recent_start if q.intent != "scoop" else (start or season_start), recent_end,
                                q.insiders or None, terms or None, q.text, limit=8)
        for p in sorted(posts, key=lambda p: p["posted_at"]):
            items.append({"kind": "P", "line": f"@{p['author']}, {_et(p['posted_at'])}: \"{_short(p['text'])}\"",
                          "url": p["url"], "t": p["posted_at"]})
    if want["claims"] and (q.players or q.insiders or q.teams or q.claim_types):
        claims = store.ask_claims(start or season_start, end or now, q.insiders or None,
                                  [p["player_id"] for p in q.players] or None, names or None,
                                  q.claim_types or None)
        if q.teams and not (q.players or q.insiders):
            claims = [c for c in claims if c.get("team") in q.teams]
        if q.intent == "scoop":
            claims = sorted(claims, key=lambda c: c["posted_at"])[:8]
        for c in claims[:12] if q.intent != "scores" else []:
            who = c["player_name"] or "a player"
            status = c["outcome"] or "not yet checked"
            orig = (f"credited @{c['credited_handle']}" if c.get("credited_handle")
                    else ("first among tracked accounts" if c.get("is_original") else "after others"))
            items.append({"kind": "C", "url": c.get("url"), "t": c["posted_at"],
                          "line": f"@{c['author']}, {_et(c['posted_at'])}: {who} {c['claim_type'].replace('_', ' ')} "
                                  f"({c['predicted_value'] or '-'}, {c['hedging']}, {orig}); outcome: {status}."})
        if q.intent == "scores" or q.insiders:
            by_author: dict[str, list] = {}
            for c in claims:
                by_author.setdefault(c["author"], []).append(c)
            scope = (", ".join(t.replace("_", " ") for t in q.claim_types) + " claims") if q.claim_types else "claims"
            label = q.window_label or "this season"
            for author, cs in by_author.items():
                s = author_stats(cs)
                correct = sum(c.get("outcome") == "correct" for c in cs)
                hit = f"raw hit rate {s['raw_hit_rate']:.0%}" if s["raw_hit_rate"] is not None else "no hit rate yet"
                items.append({"kind": "S", "url": None, "t": now,
                              "line": f"@{author}, {scope} {label}: {s['n_claims']} claims, {s['n_resolved']} resolved, "
                                      f"{correct} correct ({hit}); smoothed accuracy {s['accuracy']:.2f}."})
    if want["scores"]:
        for r in store.ask_scores(q.insiders or None, limit=10):
            if q.insiders or r["score_window"] == "season":
                items.append({"kind": "S", "url": None, "t": r["computed_at"],
                              "line": f"@{r['author']} ({r['score_window']}, {r['season']}, all claim types): credibility "
                                      f"{r['credibility_weight']:.2f}, accuracy {r['accuracy']:.2f} over "
                                      f"{r['n_resolved']} resolved of {r['n_claims']} claims"
                                      + (f", scoop rate {r['scoop_rate']:.2f}" if r["scoop_rate"] is not None else "") + "."})
    if want["predictions"]:
        upcoming = [gid for gid, x in games.items() if team_set & {x["home"], x["away"]} and x["kickoff_at"] > now]
        for p in store.ask_predictions(upcoming[:4]):
            gm = games.get(p["game_id"])
            mk = (f"; market {p['market_prob_at_time']:.1%} at that time" if p.get("market_prob_at_time") is not None else "")
            items.append({"kind": "G", "url": None, "t": p["made_at"],
                          "line": f"{_matchup(gm)} ({_et(gm['kickoff_at']) if gm else ''}): model P({gm['home'] if gm else 'home'} "
                                  f"win) {p['home_win_prob']:.1%}{mk} (model {p['model_version']}, made {_et(p['made_at'])})."})
    counters: dict[str, int] = {}
    for it in items:
        counters[it["kind"]] = counters.get(it["kind"], 0) + 1
        it["id"] = f"{it['kind']}{counters[it['kind']]}"
    return items
