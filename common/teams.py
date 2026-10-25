"""Team identity across sources. nflverse abbreviations are canonical."""
from __future__ import annotations

# nflverse abbr -> (full name used by The Odds API, nickname)
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona Cardinals", "Cardinals"),
    "ATL": ("Atlanta Falcons", "Falcons"),
    "BAL": ("Baltimore Ravens", "Ravens"),
    "BUF": ("Buffalo Bills", "Bills"),
    "CAR": ("Carolina Panthers", "Panthers"),
    "CHI": ("Chicago Bears", "Bears"),
    "CIN": ("Cincinnati Bengals", "Bengals"),
    "CLE": ("Cleveland Browns", "Browns"),
    "DAL": ("Dallas Cowboys", "Cowboys"),
    "DEN": ("Denver Broncos", "Broncos"),
    "DET": ("Detroit Lions", "Lions"),
    "GB": ("Green Bay Packers", "Packers"),
    "HOU": ("Houston Texans", "Texans"),
    "IND": ("Indianapolis Colts", "Colts"),
    "JAX": ("Jacksonville Jaguars", "Jaguars"),
    "KC": ("Kansas City Chiefs", "Chiefs"),
    "LA": ("Los Angeles Rams", "Rams"),
    "LAC": ("Los Angeles Chargers", "Chargers"),
    "LV": ("Las Vegas Raiders", "Raiders"),
    "MIA": ("Miami Dolphins", "Dolphins"),
    "MIN": ("Minnesota Vikings", "Vikings"),
    "NE": ("New England Patriots", "Patriots"),
    "NO": ("New Orleans Saints", "Saints"),
    "NYG": ("New York Giants", "Giants"),
    "NYJ": ("New York Jets", "Jets"),
    "PHI": ("Philadelphia Eagles", "Eagles"),
    "PIT": ("Pittsburgh Steelers", "Steelers"),
    "SEA": ("Seattle Seahawks", "Seahawks"),
    "SF": ("San Francisco 49ers", "49ers"),
    "TB": ("Tampa Bay Buccaneers", "Buccaneers"),
    "TEN": ("Tennessee Titans", "Titans"),
    "WAS": ("Washington Commanders", "Commanders"),
}

# Codes other sources use that differ from nflverse.
ALIASES: dict[str, str] = {
    "JAC": "JAX",   # Kalshi
    "LAR": "LA",    # Kalshi, many books
    "WSH": "WAS",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LA",
}

_BY_FULL_NAME = {full.lower(): abbr for abbr, (full, _) in TEAMS.items()}


def canon(code: str) -> str:
    """Return the nflverse abbreviation for any known team code."""
    code = code.upper().strip()
    code = ALIASES.get(code, code)
    if code not in TEAMS:
        raise KeyError(f"unknown team code: {code}")
    return code


def from_full_name(name: str) -> str:
    return _BY_FULL_NAME[name.lower().strip()]


def split_pair(codes: str) -> tuple[str, str] | None:
    """Split a concatenated pair like 'CHISEA' or 'LACKC' into two team codes.

    Returns the pair in the order it appears, or None when no unique split exists.
    """
    codes = codes.upper()
    hits = []
    for i in (2, 3):
        a, b = codes[:i], codes[i:]
        try:
            hits.append((canon(a), canon(b)))
        except KeyError:
            continue
    return hits[0] if len(hits) == 1 else None
