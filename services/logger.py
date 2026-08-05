"""
Julie ChenBot Logging Service
=============================

Centralized logging for Julie ChenBot.
"""

from __future__ import annotations

import logging
from logging import Logger

from config import LOG_FILE


class ProductionLogger:
    """
    Provides configured loggers throughout Julie ChenBot.
    """

    _formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    @classmethod
    def get(cls, name: str) -> Logger:

        logger = logging.getLogger(name)

        if logger.handlers:
            return logger

        logger.setLevel(logging.INFO)
        logger.propagate = False

        console = logging.StreamHandler()
        console.setFormatter(cls._formatter)

        file_handler = logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        )

        file_handler.setFormatter(cls._formatter)

        logger.addHandler(console)
        logger.addHandler(file_handler)

        return logger