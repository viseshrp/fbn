"""Bounded, private visual evidence captured before failing browser tabs close."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Page

from .exceptions import AccountActionRequiredError, AuthenticationRequiredError
from .logging import get_logger

LOGGER = get_logger("screenshots")
MAX_FAILURE_EVENTS = 10
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
SCREENSHOT_TIMEOUT_MS = 3_000
_EVENT_NAME = re.compile(r"\d{8}T\d{12}Z-[a-f0-9]{12}\Z")
_PAGE_ROLES = ("post", "feed")
_ATTEMPTED = "_fbn_failure_screenshots_attempted"
_INPUTS = (
    'input, textarea, [contenteditable]:not([contenteditable="false"]), '
    '[role="textbox"]'
)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise OSError("Screenshot storage must be a real directory.")
    path.chmod(0o700)


def _private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as destination:
        destination.write(data)


def _prune(root: Path) -> None:
    events = sorted(
        (
            child
            for child in root.iterdir()
            if _EVENT_NAME.fullmatch(child.name) and stat.S_ISDIR(child.lstat().st_mode)
        ),
        key=lambda child: child.name,
    )
    # Reserve one slot before writing, so a failed capture cannot grow storage.
    for child in events[: max(0, len(events) - MAX_FAILURE_EVENTS + 1)]:
        shutil.rmtree(child)


def capture_failure_screenshots(
    profile_dir: Path, error: Exception, pages: Mapping[str, Page | None]
) -> None:
    """Save at most two viewports per failure; never replace the original error.

    Images can contain private group content. Keep them outside the browser
    profile, never attach them to notifications, and omit page URLs and error
    messages from both logs and the small metadata file. Authentication and
    checkpoint screens are deliberately excluded, even with input masking.
    """
    try:
        if getattr(error, _ATTEMPTED, False):
            return
        setattr(error, _ATTEMPTED, True)
        if isinstance(error, (AuthenticationRequiredError, AccountActionRequiredError)):
            LOGGER.warning("Failure screenshots skipped for authentication privacy")
            return
        if not any(pages.get(role) is not None for role in _PAGE_ROLES):
            return
        root = profile_dir.parent / "failure-screenshots"
        _private_directory(root)
        _prune(root)
        observed_at = datetime.now(timezone.utc)
        event_name = observed_at.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:12]
        event = root / event_name
        event.mkdir(mode=0o700)
        results: dict[str, str] = {}
        for role in _PAGE_ROLES:
            page = pages.get(role)
            if page is None:
                continue
            try:
                if page.is_closed():
                    results[role] = "closed"
                    continue
                image = page.screenshot(
                    type="png",
                    full_page=False,
                    scale="css",
                    timeout=SCREENSHOT_TIMEOUT_MS,
                    mask=[page.locator(_INPUTS)],
                    mask_color="#000000",
                )
                if len(image) > MAX_SCREENSHOT_BYTES:
                    results[role] = "too_large"
                    continue
                _private_file(event / f"{role}.png", image)
                results[role] = "saved"
            except Exception:
                # A closed/crashed browser or full disk must not hide the cause.
                results[role] = "failed"
        _private_file(
            event / "metadata.json",
            json.dumps(
                {
                    "observed_at": observed_at.isoformat(),
                    "failure_category": type(error).__name__,
                    "pages": results,
                },
                sort_keys=True,
            ).encode("utf-8"),
        )
        LOGGER.warning(
            "Private failure screenshots recorded",
            screenshot_event=event_name,
            saved_count=sum(value == "saved" for value in results.values()),
            unavailable_count=sum(value != "saved" for value in results.values()),
        )
    except Exception:
        with suppress(Exception):
            LOGGER.warning("Failure screenshots unavailable; original error preserved")
