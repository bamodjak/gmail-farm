"""Logging setup: console + rotating file, one place, no surprises."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_file: str, verbose: bool = False) -> logging.Logger:
    """Configure root logging (console INFO, file DEBUG) and return a logger."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Avoid duplicate handlers on repeated setup calls.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(console)

    if log_file:
        log_dir = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        root.addHandler(fh)

    return logging.getLogger("gmail_farm")
