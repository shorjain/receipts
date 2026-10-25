from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_moves_endpoints_read_the_snapshot(tmp_path, monkeypatch):
    from store.duckdb import DuckDBStore
    snap = tmp_path / "snap.duckdb"
    s = DuckDBStore(snap)
    s.create_schema()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s.upsert_line_moves([{"move_id": "m1", "game_id": "g", "market_type": "winner",
                          "window_start": now - timedelta(hours=1), "window_end": now - timedelta(minutes=30),
                          "explain_status": "unexplained"}])
    s.close()
    monkeypatch.setenv("UI_SNAPSHOT_PATH", str(snap))
    from api.main import app
    c = TestClient(app)
    assert [m["move_id"] for m in c.get("/moves").json()] == ["m1"]
    assert c.get("/moves/m1").json()["posts"] == []
    assert c.get("/moves/missing").status_code == 404
