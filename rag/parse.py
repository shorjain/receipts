"""Rule-based question parsing for the Ask chatbot: teams, players, insiders, claim types,
time window and intent. No model involved, so the filters are predictable and testable.

  "Why did the Bills line drop this morning?"   -> team BUF, window this morning, intent moves
  "Who broke the Kelce news first?"             -> player Travis Kelce, intent scoop
  "How accurate has Russini been on injury calls this season?"
                                                -> insider DMRussini, types injury, window season, intent scores
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common import teams

ET = ZoneInfo("America/New_York")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
INJURY_TYPES = ["active", "inactive", "questionable_will_play", "return_date"]
TYPE_WORDS = {
    "injur": INJURY_TYPES, "inactive": ["inactive"], "ruled out": ["inactive"], "active": ["active"],
    "start": ["starter"], "trade": ["trade"], "sign": ["signing"], "release": ["release"], "cut": ["release"],
    "return": ["return_date"],
}
INTENTS = [  # first match wins
    ("scores", r"\b(accura|trust|credib|reliab|hit rate|track record|how good|best insider|leaderboard)"),
    ("scoop", r"\b(first|broke|break|scoop|earliest|who reported|who had)\b"),
    ("predictions", r"\b(model|predict|win probab|chance|odds of winning|favorite|edge)\b"),
    ("moves", r"\b(line|move|moved|drop|dropped|jump|spread|odds|total|kalshi|polymarket|market|why)\b"),
]
# Words that are also team abbreviations; only count them in upper case.
AMBIGUOUS_ABBR = {"NO", "LA", "SF", "NE", "GB", "TB", "KC", "LV"}


@dataclass
class Query:
    text: str
    teams: list[str] = field(default_factory=list)
    players: list[dict] = field(default_factory=list)     # {player_id, full_name, team}
    insiders: list[str] = field(default_factory=list)     # handles
    claim_types: list[str] = field(default_factory=list)
    start: datetime | None = None                         # naive UTC
    end: datetime | None = None
    window_label: str = ""
    intent: str = "any"
    week: int | None = None


def _et(now_utc: datetime) -> datetime:
    return now_utc.replace(tzinfo=timezone.utc).astimezone(ET)


def _utc(dt_et: datetime) -> datetime:
    return dt_et.astimezone(timezone.utc).replace(tzinfo=None)


def parse_window(text: str, now: datetime, season_start: datetime, week_ranges: dict[int, tuple[datetime, datetime]]
                 ) -> tuple[datetime | None, datetime | None, str, int | None]:
    t, et = text.lower(), _et(now)
    midnight = et.replace(hour=0, minute=0, second=0, microsecond=0)
    if m := re.search(r"\bweek\s*(\d{1,2})\b", t):
        w = int(m.group(1))
        if w in week_ranges:
            return *week_ranges[w], f"week {w}", w
    if m := re.search(r"\b(?:last|past)\s+(\d+)\s*(hour|hr|day|week)s?\b", t):
        n, unit = int(m.group(1)), m.group(2)
        delta = {"hour": timedelta(hours=n), "hr": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
        return now - delta, now, f"last {n} {unit}s", None
    rules = [
        ("this morning", lambda: (midnight.replace(hour=5), min(et, midnight.replace(hour=12)))),
        ("this afternoon", lambda: (midnight.replace(hour=12), min(et, midnight.replace(hour=18)))),
        ("tonight", lambda: (midnight.replace(hour=17), et)),
        ("last night", lambda: (midnight - timedelta(hours=7), midnight + timedelta(hours=4))),
        ("yesterday", lambda: (midnight - timedelta(days=1), midnight)),
        ("today", lambda: (midnight, et)),
        ("this week", lambda: (midnight - timedelta(days=et.weekday()), et)),
        ("last week", lambda: (midnight - timedelta(days=et.weekday() + 7), midnight - timedelta(days=et.weekday()))),
    ]
    for phrase, fn in rules:
        if phrase in t:
            a, b = fn()
            return _utc(a), _utc(b), phrase, None
    for i, day in enumerate(WEEKDAYS):
        if re.search(rf"\b(on\s+)?{day}\b", t):
            back = (et.weekday() - i) % 7
            d = midnight - timedelta(days=back)
            return _utc(d), _utc(min(et, d + timedelta(days=1))), day, None
    if re.search(r"\b(this|the|all)\s+season\b|\bseason\b", t):
        return season_start, now, "this season", None
    return None, None, "", None


def parse(text: str, now: datetime, roster: list[dict], insiders: list[dict], season_start: datetime,
          week_ranges: dict[int, tuple[datetime, datetime]] | None = None) -> Query:
    q = Query(text=text)
    low = text.lower()
    # teams: nicknames, full names, cities, and abbreviations
    found = []
    for abbr, (full, nick) in teams.TEAMS.items():
        city = full[: -len(nick)].strip()
        hit = re.search(rf"\b{re.escape(nick.lower())}\b", low) or re.search(rf"\b{re.escape(full.lower())}\b", low)
        if not hit and city.lower() not in ("new york", "los angeles"):
            hit = re.search(rf"\b{re.escape(city.lower())}\b", low)
        if not hit:
            hit = re.search(rf"\b{abbr}\b", text) if abbr in AMBIGUOUS_ABBR else re.search(rf"\b{abbr.lower()}\b", low)
        if hit:
            found.append((hit.start(), abbr))
    q.teams = [a for _, a in sorted(found)]
    # players first: full name, or a last name only one rostered player has (and that
    # isn't a tracked insider's last name)
    insider_lasts = {r.get("name", "").split()[-1].lower() for r in insiders if r.get("name")}
    last_counts: dict[str, int] = {}
    for p in roster:
        last = p["full_name"].split()[-1].lower()
        last_counts[last] = last_counts.get(last, 0) + 1
    seen, player_words = set(), set()
    for p in roster:
        full, last = p["full_name"].lower(), p["full_name"].split()[-1].lower()
        if p["player_id"] in seen:
            continue
        if full in low or (len(last) > 3 and last_counts[last] == 1 and last not in insider_lasts
                           and re.search(rf"\b{re.escape(last)}\b", low)):
            q.players.append({"player_id": p["player_id"], "full_name": p["full_name"], "team": p["team"]})
            seen.add(p["player_id"])
            player_words |= set(full.split()) if full in low else {last}
    # insiders: handle, full name, or a last name not already used by a player's name
    # ("Lamar Jackson" must not pull in reporter Zac Jackson)
    for r in insiders:
        name = r.get("name", "")
        last = name.split()[-1].lower() if name else ""
        if (re.search(rf"@?\b{re.escape(r['handle'].lower())}\b", low) or (name and name.lower() in low)
                or (len(last) > 3 and last not in player_words and re.search(rf"\b{re.escape(last)}\b", low))):
            q.insiders.append(r["handle"])
    for word, types in TYPE_WORDS.items():
        if word in low:
            q.claim_types += [t for t in types if t not in q.claim_types]
    q.start, q.end, q.window_label, q.week = parse_window(text, now, season_start, week_ranges or {})
    q.intent = next((name for name, pat in INTENTS if re.search(pat, low)), "any")
    return q
