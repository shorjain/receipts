"""Read-only probe of Polymarket's public Gamma API for NFL markets. Optional source:
check current US availability and docs before relying on it.

Run:  python -m scripts.probe_polymarket
"""
import httpx

from scripts._fixture import save


def main() -> None:
    r = httpx.get("https://gamma-api.polymarket.com/events",
                  params={"tag_slug": "nfl", "closed": "false", "limit": 20}, timeout=20)
    r.raise_for_status()
    events = r.json()
    print(f"{len(events)} open NFL events")
    for e in events[:5]:
        print(" -", e.get("title"))
    save("polymarket_events.json", events[:3])


if __name__ == "__main__":
    main()
