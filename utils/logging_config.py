"""Logging setup shared by every entry point.

Call `configure_logging()` once at the start of a script. Library modules can
just do `logger = logging.getLogger(__name__)` and inherit the config.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import SETTINGS

_CONFIGURED: bool = False


def configure_logging(level: str | None = None) -> None:
    """Configure root logger with console + rotating file handlers.

    Safe to call multiple times; only the first call installs handlers.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = (level or SETTINGS.log_level).upper()
    root = logging.getLogger()
    root.setLevel(log_level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler keeps logs from growing without bound.
    file_handler = RotatingFileHandler(
        SETTINGS.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _CONFIGURED = True
    logging.getLogger(__name__).debug(
        "Logging configured (level=%s, file=%s)", log_level, SETTINGS.log_file
    )
