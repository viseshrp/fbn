"""Configuration values, path resolution, and strict schedule parsing."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import platformdirs

from .exceptions import ConfigurationError

SUPPORTED_BROWSERS = frozenset({"chrome", "chromium", "msedge", "executable"})
DEFAULT_EVERY = "1h"
DEFAULT_TO = "3h"
MINIMUM_INTERVAL = timedelta(minutes=15)
MAXIMUM_INTERVAL = timedelta(days=365)

_DURATION_PATTERN = re.compile(r"([1-9][0-9]*)([smhdw])")
_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_duration(value: str) -> timedelta:
    """Parse a positive duration such as ``15m`` using a full-string match."""

    if not isinstance(value, str):
        raise ConfigurationError("duration must be a string")
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise ConfigurationError(
            "duration must be a positive integer followed by s, m, h, d, or w"
        )
    amount = int(match.group(1))
    unit = _DURATION_UNITS[match.group(2)]
    try:
        return timedelta(**{unit: amount})
    except OverflowError as exc:
        raise ConfigurationError("duration is too large") from exc


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    """A normalized inclusive scheduling interval."""

    every: timedelta = timedelta(hours=1)
    to: timedelta = timedelta(hours=3)

    def __post_init__(self) -> None:
        if not isinstance(self.every, timedelta) or not isinstance(self.to, timedelta):
            raise ConfigurationError("schedule bounds must be timedeltas")
        if self.every < MINIMUM_INTERVAL:
            raise ConfigurationError("monitor intervals must be at least 15 minutes")
        if self.to < self.every:
            raise ConfigurationError("--to must be greater than or equal to --every")
        if self.to > MAXIMUM_INTERVAL:
            raise ConfigurationError("monitor intervals must not exceed 365 days")

    @classmethod
    def from_values(
        cls,
        every: str | None = None,
        to: str | None = None,
    ) -> ScheduleSettings:
        lower, upper = parse_interval_range(every, to)
        return cls(lower, upper)


def parse_interval_range(
    every: str | None = None,
    to: str | None = None,
) -> tuple[timedelta, timedelta]:
    """Normalize CLI interval strings and enforce the scheduling requirements."""

    if every is None:
        if to is not None:
            raise ConfigurationError("--to requires --every")
        lower = parse_duration(DEFAULT_EVERY)
        upper = parse_duration(DEFAULT_TO)
    else:
        lower = parse_duration(every)
        upper = lower if to is None else parse_duration(to)

    if lower < MINIMUM_INTERVAL:
        raise ConfigurationError("monitor intervals must be at least 15 minutes")
    if upper < lower:
        raise ConfigurationError("--to must be greater than or equal to --every")
    if upper > MAXIMUM_INTERVAL:
        raise ConfigurationError("monitor intervals must not exceed 365 days")
    return lower, upper


def default_data_dir() -> Path:
    """Return the platform-specific application data directory."""

    return Path(platformdirs.user_data_path("fbn", appauthor=False))


def default_profile_dir() -> Path:
    """Return the default dedicated browser profile directory."""

    return default_data_dir() / "profile"


def default_state_file() -> Path:
    """Return the default SQLite state path."""

    return default_data_dir() / "state.sqlite3"


def _resolve_path(value: str | os.PathLike[str]) -> Path:
    if isinstance(value, str) and not value.strip():
        raise ConfigurationError("path must not be empty")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError("path could not be expanded or resolved") from exc


def resolve_profile_dir(
    value: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit, environment, then platform-default profile paths."""

    environment = os.environ if environ is None else environ
    selected = value
    if selected is None:
        selected = environment.get("FBN_PROFILE_DIR")
    return _resolve_path(default_profile_dir() if selected is None else selected)


def resolve_state_file(
    value: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit, environment, then platform-default state paths."""

    environment = os.environ if environ is None else environ
    selected = value
    if selected is None:
        selected = environment.get("FBN_STATE_FILE")
    path = _resolve_path(default_state_file() if selected is None else selected)
    if path.exists() and path.is_dir():
        raise ConfigurationError("state file path points to a directory")
    return path


def ensure_private_directory(path: str | os.PathLike[str]) -> Path:
    """Create a directory and restrict it to its owner on Unix."""

    resolved = _resolve_path(path)
    if resolved.exists() and not resolved.is_dir():
        raise ConfigurationError(f"directory path points to a file: {resolved}")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        resolved.chmod(0o700)
    return resolved


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """Validated persistent-browser launch configuration."""

    browser: str = "chromium"
    profile_dir: Path = field(default_factory=resolve_profile_dir)
    headless: bool = True
    executable_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.browser, str) or self.browser not in SUPPORTED_BROWSERS:
            choices = ", ".join(sorted(SUPPORTED_BROWSERS))
            raise ConfigurationError(f"browser must be one of: {choices}")
        if not isinstance(self.headless, bool):
            raise ConfigurationError("headless must be a boolean")

        profile_dir = _resolve_path(self.profile_dir)
        if profile_dir.exists() and not profile_dir.is_dir():
            raise ConfigurationError("profile directory path points to a file")
        object.__setattr__(self, "profile_dir", profile_dir)

        executable_path = self.executable_path
        if executable_path is not None:
            executable_path = _resolve_path(executable_path)
            if executable_path.exists() and executable_path.is_dir():
                raise ConfigurationError("executable path points to a directory")
            object.__setattr__(self, "executable_path", executable_path)

        if self.browser == "executable" and executable_path is None:
            raise ConfigurationError("--browser executable requires --executable-path")
        if self.browser != "executable" and executable_path is not None:
            raise ConfigurationError("--executable-path requires --browser executable")

    @classmethod
    def from_values(
        cls,
        *,
        browser: str = "chromium",
        profile_dir: str | os.PathLike[str] | None = None,
        headless: bool = True,
        executable_path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> BrowserSettings:
        """Build settings from CLI-like values and environment overrides."""

        return cls(
            browser=browser,
            profile_dir=resolve_profile_dir(profile_dir, environ=environ),
            headless=headless,
            executable_path=(
                None if executable_path is None else _resolve_path(executable_path)
            ),
        )
