"""Hit Kalshi's public market data once per NFL series and save fixtures for one game.

Run:  python -m scripts.probe_kalshi
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from scripts._fixture import save
from sources.kalshi import SERIES, KalshiClient


def main() -> None:
    c = KalshiClient()
    raw = {s: c.list_markets(s) for s in SERIES}
    for s, ms in raw.items():
        print(f"{s}: {len(ms)} open markets")
    # Pick the game with the most spread/total rungs so the fixture has a full ladder.
    suffix = lambda m: m["event_ticker"].split("-", 1)[1]
    games = {suffix(m) for m in raw["KXNFLGAME"]}
    rungs = Counter(suffix(m) for s in ("KXNFLSPREAD", "KXNFLTOTAL") for m in raw[s] if suffix(m) in games)
    if not rungs:
        print("no open NFL games")
        return
    ev = rungs.most_common(1)[0][0]
    print(f"fixture game: {ev}")
    for s, ms in raw.items():
        save(f"kalshi_{s.lower()}.json", [m for m in ms if suffix(m) == ev])
    game = next(m for m in raw["KXNFLGAME"] if suffix(m) == ev)
    now = datetime.now(timezone.utc)
    candles = c.candlesticks("KXNFLGAME", game["ticker"], now - timedelta(hours=6), now, 60)
    save("kalshi_candlesticks.json", {"ticker": game["ticker"], "candlesticks": candles})


if __name__ == "__main__":
    main()
