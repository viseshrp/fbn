from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

from fbn.config import (
    BrowserSettings,
    ScheduleSettings,
    ensure_private_directory,
    parse_duration,
    parse_interval_range,
    resolve_profile_dir,
    resolve_state_file,
)
from fbn.exceptions import ConfigurationError
from fbn.models import ScanPolicy


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1s", timedelta(seconds=1)),
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("3d", timedelta(days=3)),
        ("4w", timedelta(weeks=4)),
    ],
)
def test_parse_duration_accepts_only_supported_complete_values(
    value: str,
    expected: timedelta,
) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0m",
        "00m",
        "01m",
        "-1h",
        "+1h",
        " 1h",
        "1h ",
        "1.5h",
        "1hour",
        "x1m",
        "1mx",
        "1M",
    ],
)
def test_parse_duration_rejects_partial_nonpositive_and_loose_values(
    value: str,
) -> None:
    with pytest.raises(ConfigurationError):
        parse_duration(value)


def test_parse_duration_rejects_values_that_overflow_timedelta() -> None:
    with pytest.raises(ConfigurationError, match="too large"):
        parse_duration(f"{10**30}w")


def test_schedule_defaults_to_one_through_three_hours() -> None:
    assert parse_interval_range() == (
        timedelta(hours=1),
        timedelta(hours=3),
    )
    assert ScheduleSettings.from_values() == ScheduleSettings()


def test_schedule_normalizes_mixed_units_and_fixed_intervals() -> None:
    assert parse_interval_range("1h", "90m") == (
        timedelta(hours=1),
        timedelta(minutes=90),
    )
    assert parse_interval_range("2h") == (
        timedelta(hours=2),
        timedelta(hours=2),
    )


@pytest.mark.parametrize(
    ("every", "to", "message"),
    [
        (None, "1h", "--to requires --every"),
        ("14m", None, "at least 15 minutes"),
        ("2h", "119m", "greater than or equal"),
        ("366d", None, "must not exceed 365 days"),
    ],
)
def test_schedule_rejects_invalid_ranges(
    every: str | None,
    to: str | None,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_interval_range(every, to)


def test_explicit_paths_override_environment_and_are_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    environment = {
        "FBN_PROFILE_DIR": str(tmp_path / "environment-profile"),
        "FBN_STATE_FILE": str(tmp_path / "environment.sqlite3"),
    }

    assert resolve_profile_dir("explicit-profile", environ=environment) == (
        tmp_path / "explicit-profile"
    )
    assert resolve_state_file("explicit.sqlite3", environ=environment) == (
        tmp_path / "explicit.sqlite3"
    )
    assert resolve_profile_dir(environ=environment) == (
        tmp_path / "environment-profile"
    )
    assert resolve_state_file(environ=environment) == (tmp_path / "environment.sqlite3")


def test_default_paths_use_platform_application_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fbn.config.platformdirs.user_data_path",
        lambda *args, **kwargs: tmp_path / "application-data",
    )

    assert resolve_profile_dir(environ={}) == tmp_path / "application-data/profile"
    assert resolve_state_file(environ={}) == (
        tmp_path / "application-data/state.sqlite3"
    )


def test_state_path_cannot_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="directory"):
        resolve_state_file(tmp_path)


def test_path_expansion_failure_is_a_fixed_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="could not be expanded"):
        resolve_profile_dir("~fbn_user_that_does_not_exist_7f39/profile")


def test_state_path_intentionally_resolves_an_existing_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "state-link.sqlite3"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable")

    assert resolve_state_file(link) == target.resolve()


def test_private_directory_is_owner_only_on_unix(tmp_path: Path) -> None:
    profile = ensure_private_directory(tmp_path / "profile")

    assert profile.is_dir()
    if os.name != "nt":
        assert profile.stat().st_mode & 0o777 == 0o700


def test_browser_settings_validate_choice_and_executable_pairing(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    executable = tmp_path / "chromium"
    settings = BrowserSettings.from_values(
        browser="executable",
        profile_dir=profile,
        executable_path=executable,
        environ={},
    )

    assert settings.browser == "executable"
    assert settings.profile_dir == profile
    assert settings.executable_path == executable

    with pytest.raises(ConfigurationError, match="browser must be one of"):
        BrowserSettings(browser="firefox", profile_dir=profile)
    with pytest.raises(ConfigurationError, match="requires --executable-path"):
        BrowserSettings(browser="executable", profile_dir=profile)
    with pytest.raises(ConfigurationError, match="requires --browser executable"):
        BrowserSettings(
            browser="chromium",
            profile_dir=profile,
            executable_path=executable,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_count", 0),
        ("sample_count", 51),
        ("sample_count", True),
        ("max_scrolls", -1),
        ("stagnant_scrolls", 0),
        ("navigation_timeout_seconds", 0),
        ("navigation_timeout_seconds", float("nan")),
        ("navigation_timeout_seconds", float("inf")),
        ("settle_seconds", -0.1),
        ("settle_seconds", float("nan")),
        ("max_post_age_seconds", 0),
        ("max_post_age_seconds", float("inf")),
        ("max_post_age_seconds", 366 * 24 * 60 * 60),
    ],
)
def test_scan_policy_rejects_unbounded_or_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        ScanPolicy(**{field: value})


def test_scan_policy_is_frozen_and_has_no_instance_dictionary() -> None:
    policy = ScanPolicy()

    assert not hasattr(policy, "__dict__")
    with pytest.raises(FrozenInstanceError):
        policy.sample_count = 20
