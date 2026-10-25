"""Local Parquet copies of every raw pull, so Snowflake is never the only copy."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .settings import raw_dir


def write_raw(source: str, rows: list[dict] | pd.DataFrame, name: str | None = None,
              when: datetime | None = None) -> Path | None:
    """Write rows to data/raw/<source>/<YYYY-MM-DD>/<name or HHMMSS>.parquet."""
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if df.empty:
        return None
    when = when or datetime.now(timezone.utc)
    # Nested values (lists/dicts) go in as JSON text so the schema stays flat.
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, (list, dict))).any():
            df[col] = df[col].map(lambda v: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v)
    out = raw_dir() / source / when.strftime("%Y-%m-%d") / f"{name or when.strftime('%H%M%S%f')}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out
