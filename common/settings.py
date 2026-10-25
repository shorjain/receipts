"""Environment-backed settings. Never log the values of *_KEY / *_PASSWORD."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def env_float(name: str, default: float) -> float:
    return float(env(name, str(default)))


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    return str(env(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


def raw_dir() -> Path:
    p = Path(env("RAW_DATA_DIR", "data/raw"))
    return p if p.is_absolute() else ROOT / p


@lru_cache
def thresholds() -> dict:
    return yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())


@lru_cache
def insiders() -> list[dict]:
    return yaml.safe_load((ROOT / "config" / "insiders.yaml").read_text())["insiders"]
