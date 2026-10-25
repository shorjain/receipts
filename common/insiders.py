"""Pick which tracked handles to search, from config/insiders.yaml."""
from __future__ import annotations

from .settings import insiders


def tracked(include_unverified: bool = False, roster: list[dict] | None = None) -> list[dict]:
    rows = insiders() if roster is None else roster
    return [r for r in rows if r.get("active", True) and (include_unverified or r.get("verified"))]


def for_teams(teams: set[str], include_unverified: bool = False,
              roster: list[dict] | None = None) -> list[str]:
    """National insiders first, then beat reporters who cover any of `teams`."""
    rows = tracked(include_unverified, roster)
    national = [r["handle"] for r in rows if r["tier"] == "national"]
    beat = [r["handle"] for r in rows if r["tier"] == "beat" and set(r.get("teams", [])) & teams]
    return national + [h for h in beat if h not in national]
