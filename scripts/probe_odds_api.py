"""Call The Odds API once (costs ~3 credits) and save a fixture. Needs ODDS_API_KEY.

Run:  python -m scripts.probe_odds_api
"""
from scripts._fixture import save
from sources.odds_api import OddsApiClient


def main() -> None:
    c = OddsApiClient()
    print(f"estimated cost: {c.estimated_cost(['h2h', 'spreads', 'totals'])} credits")
    events = c.odds()
    print(f"{len(events)} events, {c.remaining} credits remaining")
    save("odds_api_odds.json", events[:3])


if __name__ == "__main__":
    main()
