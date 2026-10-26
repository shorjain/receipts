"""Re-check every stored move's linked posts with common/relevance.py, drop posts that
aren't about the move's game, and queue changed moves for a fresh explanation.

The background loop holds the DuckDB file, so stop it first:
  python -m scripts.service uninstall && python -m scripts.relink_moves && python -m scripts.service install
"""
from __future__ import annotations

from common.relevance import about_game
from pipelines.search_moves import relevance_index
from store import get_store


def main() -> None:
    store = get_store()
    store.create_schema()
    games = {g["game_id"]: g for g in store.get_games()}
    moves = store.line_moves()
    changed = dropped = kept = 0
    for m in moves:
        g = games.get(m["game_id"])
        links = store.move_posts(m["move_id"])
        if not g or not links:
            continue
        terms = relevance_index(store, g["season"]).terms(g)
        bad = [l["post_id"] for l in links if not about_game(l["text"], l["author"], terms)]
        kept += len(links) - len(bad)
        if bad:
            store.delete_move_posts(m["move_id"], bad)
            dropped += len(bad)
            changed += 1
            if m["explain_status"] in ("explained", "unclear", "refused", "unexplained"):
                store.upsert_line_moves([{"move_id": m["move_id"], "explain_status": "searched", "explanation": None}])
    print(f"{len(moves)} moves checked: {dropped} off-topic post links removed from {changed} moves, "
          f"{kept} links kept; changed moves will be re-explained by the loop")
    store.close()


if __name__ == "__main__":
    main()
