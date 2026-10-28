"""Logging configuration and initialization for Mountain API Gateway."""

from __future__ import annotations

import logging
import sys

from gateway.logging.formatter import ConsoleLogFormatter, JSONLogFormatter

logger = logging.getLogger("gateway.logging")


def setup_logging(
    log_level: str = "INFO",
    log_format: str = "console",
    enable_colors: bool = True,
) -> None:
    """Configures centralized structured logging for the application.

    Args:
        log_level: Minimum severity level ("DEBUG", "INFO", "WARNING", "ERROR").
        log_format: Formatting engine to use ("console" or "json").
        enable_colors: Whether to colorize console output in interactive terminals.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = log_format.lower().strip()

    if fmt == "json":
        formatter: logging.Formatter = JSONLogFormatter()
    else:
        formatter = ConsoleLogFormatter(enable_colors=enable_colors)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(level)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear pre-existing handlers to prevent duplicate lines
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    root_logger.addHandler(handler)

    # Attach to third-party loggers (uvicorn, fastapi)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        sub_logger = logging.getLogger(logger_name)
        sub_logger.setLevel(level)
        for h in list(sub_logger.handlers):
            sub_logger.removeHandler(h)
        sub_logger.addHandler(handler)
        sub_logger.propagate = False

    logger.debug("Structured logging initialized (level=%s, format=%s)", log_level, fmt)
