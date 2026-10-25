"""Shared helper for probe scripts: save one response as a test fixture."""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def save(name: str, payload) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    p = FIXTURES / name
    p.write_text(json.dumps(payload, indent=1, default=str))
    print(f"saved {p.relative_to(FIXTURES.parent.parent)}")
    return p
