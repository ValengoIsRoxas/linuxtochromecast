"""Logging a archivo: para cuando algo falla lejos de la terminal.

La terminal se limpia con cada \\r de `progress()`, así que cualquier pista de
por qué se cortó el casteo (Content-Length mal estimado, ffmpeg muriendo,
error del receptor) se pierde. Este logger escribe a un archivo persistente en
vez de a stdout, para poder mirarlo después de que ya pasó.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "ltc" / "ltc.log"

_configured = False


def setup_logging(level: int = logging.INFO) -> Path:
    """Prepara el logger `ltc`, escribiendo a `LOG_PATH`. Idempotente."""
    global _configured
    if _configured:
        return LOG_PATH

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))

    logger = logging.getLogger("ltc")
    logger.setLevel(level)
    logger.addHandler(handler)
    _configured = True
    return LOG_PATH


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ltc.{name}")
