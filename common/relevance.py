"""Is a post about a given game? Decides which posts get linked to a line move.

A post counts for a game when it
  - names either team: nickname or city (also inside hashtags like #ChiefsKingdom), or an
    abbreviation that isn't also an English word ("KC", "DEN", but not "NO" or "LA");
  - names a player on either roster: full name, or a last name that only one player in
    the league has ("Skattebo" yes; "Allen" or "Jackson" no); or
  - comes from a beat reporter who covers either team (they post about their team all
    day without always naming it).
National insiders post about every team, so time alone isn't enough: without this, a
Rapoport post about a Giants running back got attached to a Broncos-Chiefs move.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from common import teams

AMBIGUOUS_ABBR = {"NO", "LA", "NE", "SF", "GB", "TB", "LV"}   # also words or too short to trust
AMBIGUOUS_CITY = {"new york", "los angeles"}


@dataclass
class GameTerms:
    team_patterns: list[re.Pattern]
    full_names: list[str]          # lower case
    last_names: list[re.Pattern]   # case-sensitive, league-unique surnames
    beat_handles: set[str]         # lower case


class RelevanceIndex:
    """League-wide roster names, built once per run and reused for every game."""

    def __init__(self, roster_rows: list[dict], insider_rows: list[dict]):
        latest: dict[str, dict] = {}
        for r in roster_rows:
            if r["player_id"] not in latest or r["week"] > latest[r["player_id"]]["week"]:
                latest[r["player_id"]] = r
        self.by_team: dict[str, list[str]] = {}
        surname_count: dict[str, int] = {}
        for r in latest.values():
            name = (r["full_name"] or "").strip()
            if not name:
                continue
            self.by_team.setdefault(r["team"], []).append(name)
            last = name.split()[-1]
            surname_count[last] = surname_count.get(last, 0) + 1
        self.unique_surnames = {s for s, n in surname_count.items() if n == 1 and len(s) >= 4 and s[:1].isupper()}
        self.beats: dict[str, set[str]] = {}
        for r in insider_rows:
            if r.get("tier") == "beat":
                for t in r.get("teams", []):
                    self.beats.setdefault(t, set()).add(r["handle"].lower())

    def terms(self, game: dict) -> GameTerms:
        pats, names, lasts, beats = [], [], [], set()
        for code in (game["home"], game["away"]):
            full, nick = teams.TEAMS[code]
            city = full[: -len(nick)].strip()
            # Capitalized or all caps only: "Bills"/"BILLS"/#BillsMafia, not "pays the bills"
            pats.append(re.compile(rf"(?<![A-Za-z])(?:{re.escape(nick)}|{re.escape(nick.upper())})"))
            if city.lower() not in AMBIGUOUS_CITY:
                pats.append(re.compile(rf"\b(?:{re.escape(city)}|{re.escape(city.upper())})\b"))
            for abbr in {code, *[a for a, c in teams.ALIASES.items() if c == code]}:
                if abbr not in AMBIGUOUS_ABBR:
                    pats.append(re.compile(rf"(?<![A-Za-z]){abbr}(?![A-Za-z])"))      # upper case only
            for n in self.by_team.get(code, []):
                names.append(n.lower())
                last = n.split()[-1]
                if last in self.unique_surnames:
                    lasts.append(re.compile(rf"\b{re.escape(last)}\b"))
            beats |= self.beats.get(code, set())
        return GameTerms(pats, names, lasts, beats)


def about_game(text: str | None, author: str | None, terms: GameTerms) -> bool:
    if author and author.lower() in terms.beat_handles:
        return True
    if not text:
        return False
    if any(p.search(text) for p in terms.team_patterns):
        return True
    low = text.lower()
    if any(n in low for n in terms.full_names):
        return True
    return any(p.search(text) for p in terms.last_names)
