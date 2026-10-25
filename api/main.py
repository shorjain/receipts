"""Read-only JSON API over the same data as the UI.  Run:  uvicorn api.main:app --reload"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException

from store import get_read_store

app = FastAPI(title="NFL Lines API")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/moves")
def moves(days: int = 7, game_id: str | None = None, status: str | None = None) -> list[dict]:
    s = get_read_store()
    try:
        return s.line_moves(_now() - timedelta(days=days), _now(), game_id=game_id, explain_status=status)
    finally:
        s.close()


@app.get("/moves/{move_id}")
def move(move_id: str) -> dict:
    s = get_read_store()
    try:
        found = [m for m in s.line_moves() if m["move_id"] == move_id]
        if not found:
            raise HTTPException(404, "move not found")
        return {**found[0], "posts": s.move_posts(move_id)}
    finally:
        s.close()
