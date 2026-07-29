"""Secret-free structured logging for terminal and container output."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_DEBUG_LEVEL = 10
_WARNING_LEVEL = 30


def get_logger(component: str) -> Any:
    """Return an fbn logger pre-bound with stable service metadata."""

    return structlog.get_logger(service="fbn", component=component)


def configure_logging(verbose: bool) -> None:
    """Emit secret-free JSON records to stdout without changing the root logger."""

    level = _DEBUG_LEVEL if verbose else _WARNING_LEVEL
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )

    # Apprise can include destination URLs and notification bodies in its own
    # standard-library records. fbn does not configure the root logger, but
    # keep this dependency disabled when an embedding process has configured it.
    logging.getLogger("apprise").disabled = True
