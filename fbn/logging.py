"""Secret-free, human-readable logging for terminal and container output."""

from __future__ import annotations

import logging as standard_logging
import sys
from typing import Any

import loguru
from loguru import logger

_FIELD_LABELS = {
    "accumulated_count": "total candidates",
    "candidate_count": "candidates",
    "chunk_count": "chunks",
    "chunk_index": "chunk",
    "cookie_count": "cookies",
    "delay_seconds": "next check in",
    "failure_number": "failure",
    "feed_item_count": "feed items",
    "group_key": "group",
    "inserted_count": "inserted",
    "interval_max_seconds": "maximum interval",
    "interval_min_seconds": "minimum interval",
    "max_scrolls": "scroll limit",
    "navigation_timeout_seconds": "navigation timeout",
    "pass_number": "pass",
    "pending_count": "pending",
    "post_count": "posts",
    "post_link_count": "post links",
    "queued_count": "queued",
    "retry_delay_seconds": "retry in",
    "sample_count": "sample limit",
    "scroll_count": "scrolls",
    "signal_number": "signal",
    "timeout_seconds": "timeout",
}
_RESERVED_FIELDS = frozenset({"component", "rendered_context", "service"})

# Keep library-style imports quiet until a CLI command explicitly configures fbn.
logger.disable("fbn")


def get_logger(component: str) -> loguru.Logger:
    """Return an fbn logger pre-bound with stable service metadata."""

    return logger.bind(service="fbn", component=component)


def _display_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        rendered = "yes" if value else "no"
    elif value is None:
        rendered = "unknown"
    else:
        rendered = str(value)

    # Preserve the one-record-per-line contract even if a future safe field
    # unexpectedly contains control characters.
    rendered = rendered.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    if key.endswith("_seconds"):
        rendered = f"{rendered}s"
    return rendered


def _human_format(record: dict[str, Any]) -> str:
    extra = record["extra"]
    fields = []
    for key, value in extra.items():
        if key in _RESERVED_FIELDS:
            continue
        label = _FIELD_LABELS.get(key, key.replace("_", " "))
        fields.append(f"{label}={_display_value(key, value)}")
    extra["rendered_context"] = f" | {', '.join(fields)}" if fields else ""

    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss!UTC} UTC</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[component]: <10}</cyan> | "
        "<level>{message}</level>{extra[rendered_context]}\n"
    )


def _is_fbn_record(record: dict[str, Any]) -> bool:
    return record["extra"].get("service") == "fbn"


def _write_stdout(message: loguru.Message) -> None:
    """Write through the active stdout so Click and pytest capture remain valid."""

    sys.stdout.write(message)
    sys.stdout.flush()


def configure_logging(verbose: bool) -> None:
    """Emit safe, readable fbn records without changing the root logger."""

    logger.configure(
        handlers=[
            {
                "sink": _write_stdout,
                "level": "DEBUG" if verbose else "WARNING",
                "format": _human_format,
                "filter": _is_fbn_record,
                "colorize": sys.stdout.isatty(),
                "backtrace": False,
                "diagnose": False,
            }
        ]
    )
    logger.enable("fbn")

    # Apprise can include destination URLs and notification bodies in its own
    # standard-library records. fbn does not configure the root logger, but
    # keep this dependency disabled when an embedding process has configured it.
    standard_logging.getLogger("apprise").disabled = True
