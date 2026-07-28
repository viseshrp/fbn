from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from playwright.sync_api import Error as PlaywrightError

import fbn.cli as cli
import fbn.diagnostics as diagnostics
from fbn.config import BrowserSettings


class FakeBrowser:
    def __init__(self, version: str = "123.0") -> None:
        self.version = version
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(
        self,
        browser: FakeBrowser,
        error: Exception | None = None,
    ) -> None:
        self.browser = browser
        self.error = error
        self.launch_options: list[dict[str, object]] = []

    def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_options.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakePlaywright:
        self.entered = True
        return self.playwright

    def __exit__(self, *args: object) -> None:
        self.exited = True


def fake_playwright_factory(
    *,
    error: Exception | None = None,
    version: str = "123.0",
) -> tuple[Any, FakeBrowser, FakeChromium, FakePlaywrightManager]:
    browser = FakeBrowser(version)
    chromium = FakeChromium(browser, error)
    manager = FakePlaywrightManager(FakePlaywright(chromium))
    return lambda: manager, browser, chromium, manager


@pytest.mark.parametrize(
    ("browser", "expected"),
    [
        ("chrome", {"headless": True, "channel": "chrome"}),
        ("chromium", {"headless": True, "channel": "chromium"}),
        ("msedge", {"headless": True, "channel": "msedge"}),
    ],
)
def test_probe_uses_the_selected_headless_browser_and_closes_it(
    tmp_path: Path,
    browser: str,
    expected: dict[str, object],
) -> None:
    factory, fake_browser, chromium, manager = fake_playwright_factory()
    settings = BrowserSettings(
        browser=browser,
        profile_dir=tmp_path / "unused-profile",
        headless=False,
    )

    available, detail = diagnostics.probe_headless_browser(
        settings,
        playwright_factory=factory,
    )

    assert available is True
    assert detail == "headless launch succeeded (123.0)"
    assert chromium.launch_options == [expected]
    assert fake_browser.closed is True
    assert manager.entered is True
    assert manager.exited is True
    assert not settings.profile_dir.exists()


def test_probe_passes_an_explicit_browser_binary(tmp_path: Path) -> None:
    factory, fake_browser, chromium, _ = fake_playwright_factory()
    executable = tmp_path / "system-chromium"
    settings = BrowserSettings(
        browser="executable",
        profile_dir=tmp_path / "unused-profile",
        executable_path=executable,
    )

    available, _ = diagnostics.probe_headless_browser(
        settings,
        playwright_factory=factory,
    )

    assert available is True
    assert chromium.launch_options == [
        {
            "headless": True,
            "executable_path": str(executable),
        }
    ]
    assert fake_browser.closed is True


def test_probe_failure_is_actionable_and_redacts_playwright_details(
    tmp_path: Path,
) -> None:
    secret = "cookie-value-that-must-not-print"
    factory, fake_browser, chromium, manager = fake_playwright_factory(
        error=PlaywrightError(secret)
    )
    settings = BrowserSettings(
        browser="chromium",
        profile_dir=tmp_path / "unused-profile",
    )

    available, detail = diagnostics.probe_headless_browser(
        settings,
        playwright_factory=factory,
    )

    assert available is False
    assert "install the selected browser" in detail
    assert secret not in detail
    assert chromium.launch_options == [{"headless": True, "channel": "chromium"}]
    assert fake_browser.closed is False
    assert manager.exited is True


def test_doctor_command_succeeds_with_fake_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, fake_browser, chromium, _ = fake_playwright_factory(version="124.5")
    original_run_doctor = diagnostics.run_doctor

    def run_with_fake(
        settings: BrowserSettings,
        *,
        state_file: str | Path | None = None,
    ) -> diagnostics.DoctorReport:
        return original_run_doctor(
            settings,
            state_file=state_file,
            playwright_factory=factory,
        )

    monkeypatch.setattr(cli, "run_doctor", run_with_fake)
    profile_dir = tmp_path / "profile"
    state_file = tmp_path / "state.sqlite3"

    result = CliRunner().invoke(
        cli.main,
        [
            "doctor",
            "--browser",
            "chromium",
            "--profile-dir",
            str(profile_dir),
            "--state-file",
            str(state_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "browser: chromium" in result.output
    assert f"profile directory: {profile_dir}" in result.output
    assert f"state file: {state_file}" in result.output
    assert "headless launch succeeded (124.5)" in result.output
    assert chromium.launch_options == [{"headless": True, "channel": "chromium"}]
    assert fake_browser.closed is True


def test_doctor_command_fails_nonzero_with_fake_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-browser-error"
    factory, _, _, _ = fake_playwright_factory(error=PlaywrightError(secret))
    original_run_doctor = diagnostics.run_doctor

    def run_with_fake(
        settings: BrowserSettings,
        *,
        state_file: str | Path | None = None,
    ) -> diagnostics.DoctorReport:
        return original_run_doctor(
            settings,
            state_file=state_file,
            playwright_factory=factory,
        )

    monkeypatch.setattr(cli, "run_doctor", run_with_fake)
    result = CliRunner().invoke(
        cli.main,
        [
            "doctor",
            "--browser",
            "chromium",
            "--profile-dir",
            str(tmp_path / "profile"),
        ],
    )

    assert result.exit_code == 3
    assert "failed its local headless launch check" in result.output
    assert "install the selected browser" in result.output
    assert secret not in result.output
