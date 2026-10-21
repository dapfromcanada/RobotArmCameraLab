"""structlog setup.

Logs go to **stderr** only. Stdout is reserved for CLI result output and the
MCP stdio protocol; nothing in camera_lab may log to stdout. The per-run JSON
sink (``steps.jsonl``) is added in Phase 2.

Level precedence: explicit argument > ``CAMERA_LAB_LOG_LEVEL`` > ``INFO``.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_level(level: str | None = None) -> int:
    """Return the numeric log level from ``level`` or the environment."""
    name = (level or os.environ.get("CAMERA_LAB_LOG_LEVEL") or "INFO").upper()
    if name not in _VALID_LEVELS:
        raise ValueError(f"invalid log level {name!r}; expected one of {', '.join(_VALID_LEVELS)}")
    return int(getattr(logging, name))


class _CurrentStderr:
    """File-like object that always writes to the *current* ``sys.stderr``.

    Binding the logger to the stderr object present at configure time breaks
    when stderr is later replaced (test runners, embedding hosts) and closed.
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: str | None = None, *, json: bool = False) -> None:
    """Configure structlog to write to stderr at the resolved level."""
    numeric = resolve_level(level)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a bound logger (call :func:`configure_logging` first)."""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger


# Safe default at import time: structlog's own default prints to *stdout*, which
# would corrupt MCP stdio and CLI output. Until a front end calls
# configure_logging(), log INFO and above to stderr.
if not structlog.is_configured():
    configure_logging("INFO")
