"""Logging a archivo: para cuando algo falla lejos de la terminal.

La terminal se limpia con cada \\r de `progress()`, así que cualquier pista de
por qué se cortó el casteo (Content-Length mal estimado, ffmpeg muriendo,
error del receptor) se pierde. Este logger escribe a un archivo persistente en
vez de a stdout, para poder mirarlo después de que ya pasó.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False
_path: Path | None = None


def setup_logging(level: int = logging.INFO) -> Path:
    """Prepara el logger `ltc`, escribiendo al log de la configuración. Idempotente."""
    global _configured, _path
    if _configured:
        return _path

    # Acá y no arriba: la configuración también usa este módulo para loguear.
    from . import config
    _path = config.get().log_path
    _path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(_path, maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))

    logger = logging.getLogger("ltc")
    logger.setLevel(level)
    logger.addHandler(handler)
    _configured = True
    return _path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ltc.{name}")
