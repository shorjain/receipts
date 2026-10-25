from __future__ import annotations

import logging

from .settings import env


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("snowflake.connector", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)
