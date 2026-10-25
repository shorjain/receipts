from __future__ import annotations

import logging

from .settings import env


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger(name)
