"""Read-only probe of Polymarket NFL game markets (Gamma catalog + CLOB books).
Saves raw Gamma markets and books for one game as fixtures.

Run:  python -m scripts.probe_polymarket [--game 2026_07_LA_LV]
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from scripts._fixture import save
from sources import polymarket as pm
from store import get_store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", help="game_id to save; default: the upcoming game with the most markets")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    games = get_store().games_between(now - timedelta(days=1), now + timedelta(days=10))
    if not games:
        raise SystemExit("load games first: python -m pipelines.load_nflverse")
    c = pm.PolymarketClient()
    raw = {smt: c.gamma_markets(smt, now - timedelta(days=1), now + timedelta(days=10)) for smt in pm.MARKET_TYPES}
    for smt, ms in raw.items():
        print(f"{smt}: {len(ms)} open NFL markets")
    cat = [e for ms in raw.values() for m in ms if (e := pm.catalog_entry(m, games))]
    upcoming = {g["game_id"] for g in games if g["kickoff_at"] > now}
    game_id = args.game or Counter(e["game_id"] for e in cat if e["game_id"] in upcoming).most_common(1)[0][0]
    keep_ids = {e["gamma_id"] for e in cat if e["game_id"] == game_id}
    print(f"fixture game: {game_id} ({len(keep_ids)} markets)")
    strip = ("description", "image", "icon", "clobRewards")
    save("polymarket_gamma_markets.json",
         [{**{k: v for k, v in m.items() if k not in strip}, "events": [{"ticker": m["events"][0]["ticker"]}]}
         for ms in raw.values() for m in ms if m["id"] in keep_ids])
    books = c.books([e["token_id"] for e in cat if e["game_id"] == game_id])
    save("polymarket_books.json", [{**b, "bids": b["bids"][-5:], "asks": b["asks"][-5:]} for b in books])


if __name__ == "__main__":
    main()
