import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

BASE_THRESHOLDS = {
    "lookback_minutes": 30, "prob_points": 0.03, "key_numbers": [3, 7, 10, 14],
    "total_points": 1.5, "spread_points": 1.0, "spread_min_change": 0.5,
    "merge_gap_minutes": 0, "min_prob_change_to_store": 0.005,
}


def load_fixture(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def cfg():
    return dict(BASE_THRESHOLDS)


@pytest.fixture
def la_lv_game():
    return {"game_id": "2026_07_LA_LV", "season": 2026, "week": 7, "home": "LV", "away": "LA",
            "kickoff_at": datetime(2026, 10, 25, 20, 25)}


@pytest.fixture
def store():
    from store.duckdb import DuckDBStore
    s = DuckDBStore(":memory:")
    s.create_schema()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _raw_to_tmp(tmp_path, monkeypatch):
    """Keep tests from writing Parquet into the real data/raw."""
    monkeypatch.setenv("RAW_DATA_DIR", str(tmp_path / "raw"))
