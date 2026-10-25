from __future__ import annotations

import logging

from .settings import env


def get_logger(name: str) -> logging.Logger:
    handlers = None
    path = env("LOG_FILE")
    if path:  # the background service logs here, rotating at 10 MB x 5 files
        from logging.handlers import RotatingFileHandler
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handlers = [RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5)]
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    for noisy in ("snowflake.connector", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)
