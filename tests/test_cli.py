from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import fbn.cli as cli
import fbn.scheduling as scheduling
from fbn.config import BrowserSettings, ScheduleSettings
from fbn.exceptions import AuthenticationRequiredError
from fbn.models import GroupRef, RunSummary, ScanPolicy


def test_root_help_and_version_list_the_supported_commands() -> None:
    runner = CliRunner()

    help_result = runner.invoke(cli.main, ["--help"])
    version_result = runner.invoke(cli.main, ["--version"])

    assert help_result.exit_code == 0
    for command in ("bootstrap", "login", "doctor", "check", "monitor"):
        assert command in help_result.output
    assert version_result.exit_code == 0
    assert "version 0.2.0" in version_result.output


def test_login_opens_a_headed_dedicated_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakePostSource:
        def __init__(self, settings: BrowserSettings) -> None:
            captured["settings"] = settings

        def interactive_login(self, wait_for_user: Any) -> None:
            captured["waited"] = True
            wait_for_user()

    monkeypatch.setattr(cli, "PlaywrightPostSource", FakePostSource)
    profile_dir = tmp_path / "profile"

    result = CliRunner().invoke(
        cli.main,
        [
            "login",
            "--browser",
            "chromium",
            "--profile-dir",
            str(profile_dir),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, BrowserSettings)
    assert settings.browser == "chromium"
    assert settings.profile_dir == profile_dir
    assert settings.headless is False
    assert captured["waited"] is True
    assert "Recovery profile saved locally" in result.output


def test_bootstrap_imports_auth_file_and_validates_headlessly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    secret = "cookie-value-must-not-print"

    class FakeState:
        value = "feed"

    class FakePostSource:
        def __init__(self, settings: BrowserSettings) -> None:
            captured["settings"] = settings

        def bootstrap_auth(
            self,
            group: GroupRef,
            cookies: object,
            *,
            navigation_timeout_seconds: float,
        ) -> FakeState:
            captured["group"] = group
            captured["cookies"] = cookies
            captured["timeout"] = navigation_timeout_seconds
            return FakeState()

    monkeypatch.setattr(cli, "PlaywrightPostSource", FakePostSource)
    auth_file = tmp_path / "facebook-auth.json"
    auth_file.write_text(
        json.dumps(
            [
                {
                    "name": "session",
                    "value": secret,
                    "domain": ".facebook.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "foreign",
                    "value": "foreign-secret",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        ),
        encoding="utf-8",
    )
    profile_dir = tmp_path / "profile"

    result = CliRunner().invoke(
        cli.main,
        [
            "bootstrap",
            "--auth-file",
            str(auth_file),
            "--id",
            "pi-group",
            "--profile-dir",
            str(profile_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, BrowserSettings)
    assert settings.browser == "chromium"
    assert settings.headless is True
    assert settings.profile_dir == profile_dir
    assert captured["group"] == GroupRef(
        "pi-group",
        "https://www.facebook.com/groups/pi-group/",
    )
    assert captured["timeout"] == 60.0
    assert len(captured["cookies"]) == 1  # type: ignore[arg-type]
    assert "imported 1 Facebook cookies" in result.output
    assert "ignored 1 non-Facebook cookie" in result.output
    assert secret not in result.output
    assert "foreign-secret" not in result.output


def test_verbose_logging_does_not_enable_dependency_or_root_debug() -> None:
    root_logger = logging.getLogger()
    apprise_logger = logging.getLogger("apprise")
    original_root_level = root_logger.level
    original_root_handlers = list(root_logger.handlers)
    original_disabled = apprise_logger.disabled
    try:
        cli._configure_logging(verbose=True)

        assert root_logger.level == original_root_level
        assert root_logger.handlers == original_root_handlers
        assert apprise_logger.disabled is True
        assert cli.LOGGER.level == logging.DEBUG
        assert cli.LOGGER.propagate is False
    finally:
        apprise_logger.disabled = original_disabled


def test_check_runs_without_release_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run_once(**kwargs: object) -> RunSummary:
        nonlocal called
        called = True
        return RunSummary(
            group_key="compose-group",
            observed=0,
            new_posts=0,
            pending=0,
            delivered=0,
            baseline=True,
        )

    monkeypatch.setattr(cli, "_run_once", fake_run_once)
    result = CliRunner().invoke(
        cli.main,
        ["check", "--id", "compose-group", "--dry-run"],
        env={"FBN_APPRISE_URL": ""},
    )

    assert result.exit_code == 0, result.output
    assert called is True
    assert "baseline: observed=0 new=0 delivered=0 pending=0" in result.output


def test_check_propagates_headless_chromium_settings_without_apprise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_once(**kwargs: object) -> RunSummary:
        captured.update(kwargs)
        return RunSummary(
            group_key="pi-group",
            observed=3,
            new_posts=1,
            pending=0,
            delivered=1,
            baseline=False,
        )

    monkeypatch.setattr(cli, "_run_once", fake_run_once)
    profile_dir = tmp_path / "profile"
    state_file = tmp_path / "state.sqlite3"

    result = CliRunner().invoke(
        cli.main,
        [
            "check",
            "--id",
            "pi-group",
            "--browser",
            "chromium",
            "--headless",
            "--profile-dir",
            str(profile_dir),
            "--state-file",
            str(state_file),
            "--sample-count",
            "7",
            "--max-scrolls",
            "3",
            "--stagnant-scrolls",
            "1",
            "--max-post-age",
            "45m",
            "--navigation-timeout",
            "45",
            "--settle-seconds",
            "0.5",
            "--dry-run",
        ],
        env={"FBN_APPRISE_URL": ""},
    )

    assert result.exit_code == 0, result.output
    group = captured["group"]
    settings = captured["settings"]
    policy = captured["policy"]
    assert isinstance(group, GroupRef)
    assert group.key == "pi-group"
    assert isinstance(settings, BrowserSettings)
    assert settings.browser == "chromium"
    assert settings.profile_dir == profile_dir
    assert settings.headless is True
    assert settings.executable_path is None
    assert policy == ScanPolicy(
        sample_count=7,
        max_scrolls=3,
        stagnant_scrolls=1,
        max_post_age_seconds=45 * 60,
        navigation_timeout_seconds=45,
        settle_seconds=0.5,
    )
    assert captured["state_file"] == state_file
    assert captured["apprise_url"] is None
    assert captured["dry_run"] is True
    assert "observation: observed=3 new=1 delivered=1 pending=0" in result.output


def test_check_requires_apprise_unless_dry_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.main,
        [
            "check",
            "--id",
            "example-group",
            "--profile-dir",
            str(tmp_path / "profile"),
        ],
        env={"FBN_APPRISE_URL": ""},
    )

    assert result.exit_code == 2
    assert "required unless --dry-run is used" in result.output


def test_monitor_propagates_headless_chromium_and_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    captured_signals: list[tuple[signal.Signals, object]] = []

    class FakePostSource:
        def __init__(self, settings: BrowserSettings) -> None:
            captured["settings"] = settings

    class FakeState:
        def __init__(self, state_file: Path | None) -> None:
            captured["state_file"] = state_file

        def __enter__(self) -> FakeState:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class FakeService:
        def __init__(self, source: object, state: object, sink: object) -> None:
            captured["source"] = source
            captured["state"] = state
            captured["sink"] = sink

    class FakeLoop:
        def __init__(
            self,
            service: object,
            state: object,
            schedule: ScheduleSettings,
            *,
            on_success: object | None = None,
        ) -> None:
            captured["loop_service"] = service
            captured["loop_state"] = state
            captured["schedule"] = schedule
            captured["on_success"] = on_success

        def run(
            self,
            group: GroupRef,
            policy: ScanPolicy,
            **kwargs: object,
        ) -> None:
            captured["group"] = group
            captured["policy"] = policy
            captured["run_kwargs"] = kwargs

    def fake_signal(signum: signal.Signals, handler: object) -> object:
        captured_signals.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(cli, "PlaywrightPostSource", FakePostSource)
    monkeypatch.setattr(cli, "SQLiteStateRepository", FakeState)
    monkeypatch.setattr(cli, "MonitorService", FakeService)
    monkeypatch.setattr(scheduling, "MonitorLoop", FakeLoop)
    monkeypatch.setattr(cli.signal, "signal", fake_signal)
    profile_dir = tmp_path / "profile"
    state_file = tmp_path / "state.sqlite3"

    result = CliRunner().invoke(
        cli.main,
        [
            "monitor",
            "--id",
            "pi-group",
            "--browser",
            "chromium",
            "--headless",
            "--profile-dir",
            str(profile_dir),
            "--state-file",
            str(state_file),
            "--every",
            "15m",
            "--to",
            "30m",
            "--dry-run",
        ],
        env={"FBN_APPRISE_URL": ""},
    )

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, BrowserSettings)
    assert settings.browser == "chromium"
    assert settings.profile_dir == profile_dir
    assert settings.headless is True
    assert captured["state_file"] == state_file
    assert captured["schedule"] == ScheduleSettings(
        every=timedelta(minutes=15),
        to=timedelta(minutes=30),
    )
    assert captured["on_success"] is cli._run_summary
    assert captured["group"] == GroupRef(
        "pi-group",
        "https://www.facebook.com/groups/pi-group/",
    )
    run_kwargs = captured["run_kwargs"]
    assert isinstance(run_kwargs, dict)
    assert run_kwargs["notify_initial"] is False
    assert run_kwargs["commit_delivery"] is False
    assert isinstance(run_kwargs["stop_event"], threading.Event)
    assert len(captured_signals) == 4
    assert "monitor stopped" in result.output


def test_typed_failure_uses_stable_nonzero_exit_and_does_not_echo_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "mailto://user:do-not-print@example.com"

    def fail(**kwargs: object) -> RunSummary:
        assert kwargs["apprise_url"] == secret
        raise AuthenticationRequiredError(
            "The dedicated browser profile is not signed in."
        )

    monkeypatch.setattr(cli, "_run_once", fail)
    result = CliRunner().invoke(
        cli.main,
        [
            "check",
            "--id",
            "example-group",
            "--apprise-url",
            secret,
        ],
    )

    assert result.exit_code == AuthenticationRequiredError.exit_code
    assert "not signed in" in result.output
    assert secret not in result.output
    assert "do-not-print" not in result.output


def test_local_state_os_error_is_redacted_and_uses_stable_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-path-token"

    def fail(**kwargs: object) -> RunSummary:
        raise PermissionError(secret)

    monkeypatch.setattr(cli, "_run_once", fail)
    result = CliRunner().invoke(
        cli.main,
        [
            "check",
            "--id",
            "example-group",
            "--dry-run",
        ],
        env={"FBN_APPRISE_URL": ""},
    )

    assert result.exit_code == 2
    assert "Local state could not be accessed" in result.output
    assert secret not in result.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--browser", "firefox"], "Invalid value"),
        (
            ["--browser", "executable"],
            "--browser executable requires --executable-path",
        ),
        (
            [
                "--browser",
                "chromium",
                "--executable-path",
                sys.executable,
            ],
            "--executable-path requires --browser executable",
        ),
    ],
)
def test_browser_and_executable_combinations_are_validated(
    arguments: list[str],
    message: str,
) -> None:
    result = CliRunner().invoke(cli.main, ["login", *arguments])

    assert result.exit_code == 2
    assert message in result.output
