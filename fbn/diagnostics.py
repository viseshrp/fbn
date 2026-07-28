"""Secret-safe installation and headless-browser diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .config import BrowserSettings, resolve_state_file


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Read-only diagnostic results safe to print to a terminal."""

    browser: str
    profile_dir: Path
    state_file: Path
    browser_available: bool
    browser_detail: str


def _launch_options(settings: BrowserSettings) -> dict[str, object]:
    options: dict[str, object] = {"headless": True}
    if settings.browser == "chrome":
        options["channel"] = "chrome"
    elif settings.browser == "msedge":
        options["channel"] = "msedge"
    elif settings.browser == "chromium":
        # The explicit channel selects regular Chromium's current headless mode
        # instead of the legacy, separate headless-shell executable.
        options["channel"] = "chromium"
    elif settings.browser == "executable":
        options["executable_path"] = str(settings.executable_path)
    return options


def probe_headless_browser(
    settings: BrowserSettings,
    *,
    playwright_factory: Callable[[], Any] = sync_playwright,
) -> tuple[bool, str]:
    """Launch and close a temporary headless browser without using the profile."""

    try:
        with playwright_factory() as playwright:
            browser = playwright.chromium.launch(**_launch_options(settings))
            try:
                version = browser.version
            finally:
                browser.close()
    except PlaywrightError:
        return (
            False,
            "launch failed; install the selected browser and its system dependencies",
        )
    return True, f"headless launch succeeded ({version})"


def run_doctor(
    settings: BrowserSettings,
    *,
    state_file: str | Path | None = None,
    playwright_factory: Callable[[], Any] = sync_playwright,
) -> DoctorReport:
    """Collect a diagnostic report without opening Facebook or the user profile."""

    available, detail = probe_headless_browser(
        settings,
        playwright_factory=playwright_factory,
    )
    return DoctorReport(
        browser=settings.browser,
        profile_dir=settings.profile_dir,
        state_file=resolve_state_file(state_file),
        browser_available=available,
        browser_detail=detail,
    )
