"""Command-line interface for the browser-backed Facebook group monitor."""

from __future__ import annotations

import functools
import logging
import signal
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import click

from . import __version__
from .auth import load_facebook_cookies
from .browser import PlaywrightPostSource
from .config import (
    SUPPORTED_BROWSERS,
    BrowserSettings,
    ScheduleSettings,
    parse_duration,
)
from .diagnostics import run_doctor
from .exceptions import (
    BootstrapInterruptedError,
    BrowserUnavailableError,
    ConfigurationError,
    FbnError,
)
from .extractor import parse_group_ref
from .models import GroupRef, RunSummary, ScanPolicy
from .monitor import MonitorService
from .notifications import AppriseSink, ConsoleSink, Notification
from .state import SQLiteStateRepository

CommandFunction = TypeVar("CommandFunction", bound=Callable[..., Any])
LOGGER = logging.getLogger("fbn")


class CliError(click.ClickException):
    """A concise Click failure retaining the domain error's exit code."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _domain_errors(function: CommandFunction) -> CommandFunction:
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except FbnError as exc:
            raise CliError(str(exc), exc.exit_code) from exc
        except ValueError as exc:
            error = ConfigurationError(str(exc))
            raise CliError(str(error), error.exit_code) from exc
        except (OSError, sqlite3.Error) as exc:
            error = ConfigurationError(
                f"Local state could not be accessed ({type(exc).__name__})."
            )
            raise CliError(str(error), error.exit_code) from exc

    return cast(CommandFunction, wrapped)


def _browser_options(function: CommandFunction) -> CommandFunction:
    options = [
        click.option(
            "--browser",
            type=click.Choice(sorted(SUPPORTED_BROWSERS), case_sensitive=False),
            default="chromium",
            envvar="FBN_BROWSER",
            show_default=True,
            show_envvar=True,
            help="Browser channel, Playwright Chromium, or explicit executable.",
        ),
        click.option(
            "--profile-dir",
            type=click.Path(
                file_okay=False,
                dir_okay=True,
                path_type=Path,
                resolve_path=True,
            ),
            envvar="FBN_PROFILE_DIR",
            show_envvar=True,
            help="Dedicated fbn browser profile directory.",
        ),
        click.option(
            "--executable-path",
            type=click.Path(
                exists=True,
                file_okay=True,
                dir_okay=False,
                executable=True,
                path_type=Path,
                resolve_path=True,
            ),
            envvar="FBN_EXECUTABLE_PATH",
            show_envvar=True,
            help="Browser binary used with --browser executable.",
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _scan_options(function: CommandFunction) -> CommandFunction:
    options = [
        click.option(
            "-s",
            "--sample-count",
            type=click.IntRange(1, 50),
            default=10,
            show_default=True,
            help="Maximum visible posts retained from one bounded scan.",
        ),
        click.option(
            "--max-scrolls",
            type=click.IntRange(0, 20),
            default=4,
            show_default=True,
            help="Maximum scroll operations per check.",
        ),
        click.option(
            "--stagnant-scrolls",
            type=click.IntRange(1, 10),
            default=2,
            show_default=True,
            help="Stop after this many scrolls reveal no additional posts.",
        ),
        click.option(
            "--max-post-age",
            default="1h",
            envvar="FBN_MAX_POST_AGE",
            show_default=True,
            show_envvar=True,
            help="Notify only posts no older than this duration.",
        ),
        click.option(
            "--navigation-timeout",
            type=click.FloatRange(min=5.0, max=180.0),
            default=30.0,
            show_default=True,
            help="Navigation timeout in seconds.",
        ),
        click.option(
            "--settle-seconds",
            type=click.FloatRange(min=0.0, max=10.0),
            default=1.0,
            show_default=True,
            help="Wait after each bounded scroll.",
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _run_options(function: CommandFunction) -> CommandFunction:
    options = [
        click.option(
            "-i",
            "--id",
            "target_id",
            required=True,
            help="Facebook group ID, slug, or canonical group URL.",
        ),
        click.option(
            "--state-file",
            type=click.Path(
                file_okay=True,
                dir_okay=False,
                path_type=Path,
                resolve_path=True,
            ),
            envvar="FBN_STATE_FILE",
            show_envvar=True,
            help="SQLite state file.",
        ),
        click.option(
            "--headless/--headed",
            default=True,
            envvar="FBN_HEADLESS",
            show_default=True,
            show_envvar=True,
            help="Run without a display (required for unattended servers).",
        ),
        click.option(
            "-a",
            "--apprise-url",
            envvar="FBN_APPRISE_URL",
            show_envvar=True,
            help="Apprise destination URL. Never logged.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            help="Print pending notifications and leave them pending.",
        ),
        click.option(
            "--notify-initial",
            is_flag=True,
            help="Notify the first successful sample instead of baselining it.",
        ),
        click.option(
            "--include-errors",
            is_flag=True,
            help="Notify a secret-free failure category when a hard error occurs.",
        ),
        click.option(
            "-v",
            "--verbose",
            is_flag=True,
            help="Enable secret-free operational logging.",
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _browser_settings(
    *,
    browser: str,
    profile_dir: Path | None,
    headless: bool,
    executable_path: Path | None,
) -> BrowserSettings:
    return BrowserSettings.from_values(
        browser=browser.lower(),
        profile_dir=profile_dir,
        headless=headless,
        executable_path=executable_path,
    )


def _scan_policy(
    *,
    sample_count: int,
    max_scrolls: int,
    stagnant_scrolls: int,
    max_post_age: str,
    navigation_timeout: float,
    settle_seconds: float,
) -> ScanPolicy:
    return ScanPolicy(
        sample_count=sample_count,
        max_scrolls=max_scrolls,
        stagnant_scrolls=stagnant_scrolls,
        max_post_age_seconds=parse_duration(max_post_age).total_seconds(),
        navigation_timeout_seconds=navigation_timeout,
        settle_seconds=settle_seconds,
    )


def _notification_sink(
    *,
    apprise_url: str | None,
    dry_run: bool,
) -> ConsoleSink | AppriseSink:
    if dry_run:
        return ConsoleSink(click.echo)
    if apprise_url is None or not apprise_url.strip():
        raise ConfigurationError(
            "--apprise-url or FBN_APPRISE_URL is required unless --dry-run is used."
        )
    return AppriseSink(apprise_url)


def _configure_logging(verbose: bool) -> None:
    """Configure only fbn's secret-free records, never dependency debug logs."""

    level = logging.DEBUG if verbose else logging.WARNING
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False

    # Apprise debug records can contain destination URLs and notification
    # bodies. Delivery failures are mapped to redacted domain errors instead.
    logging.getLogger("apprise").disabled = True


def _safe_error_notification(
    *,
    apprise_url: str | None,
    group: GroupRef,
    error: FbnError,
) -> None:
    if apprise_url is None or not apprise_url.strip():
        return
    category = type(error).__name__
    notification = Notification(
        title=f"fbn monitor stopped for {group.key}",
        body=(
            f"Failure category: {category}\n"
            "Run `fbn doctor` and refresh the `fbn bootstrap` authentication "
            "file when authentication is no longer valid."
        ),
    )
    try:
        AppriseSink(apprise_url).send(notification)
    except FbnError:
        LOGGER.warning("The optional error notification also failed.")


def _run_summary(summary: RunSummary) -> None:
    mode = "baseline" if summary.baseline else "observation"
    click.echo(
        f"{mode}: observed={summary.observed} new={summary.new_posts} "
        f"delivered={summary.delivered} pending={summary.pending}"
    )


def _run_once(
    *,
    group: GroupRef,
    settings: BrowserSettings,
    policy: ScanPolicy,
    state_file: Path | None,
    apprise_url: str | None,
    dry_run: bool,
    notify_initial: bool,
) -> RunSummary:
    source = PlaywrightPostSource(settings)
    sink = _notification_sink(apprise_url=apprise_url, dry_run=dry_run)
    with SQLiteStateRepository(state_file) as state:
        service = MonitorService(source, state, sink)
        return service.run_once(
            group,
            policy,
            notify_initial=notify_initial,
            commit_delivery=not dry_run,
        )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version")
def main() -> None:
    """Monitor a Facebook group through a dedicated local browser profile."""


@main.command("bootstrap")
@_browser_options
@click.option(
    "--auth-file",
    required=True,
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        path_type=Path,
        resolve_path=True,
    ),
    envvar="FBN_AUTH_FILE",
    show_envvar=True,
    help="Secret-mounted cookie export or Playwright storage-state file.",
)
@click.option(
    "-i",
    "--id",
    "target_id",
    required=True,
    envvar="FBN_GROUP",
    show_envvar=True,
    help="Facebook group ID, slug, or canonical group URL to validate.",
)
@click.option(
    "--navigation-timeout",
    type=click.FloatRange(min=5.0, max=180.0),
    default=60.0,
    show_default=True,
    help="Headless authentication validation timeout in seconds.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable secret-free operational logging.",
)
@_domain_errors
def bootstrap_command(
    *,
    browser: str,
    profile_dir: Path | None,
    executable_path: Path | None,
    auth_file: Path,
    target_id: str,
    navigation_timeout: float,
    verbose: bool,
) -> None:
    """Import a secret auth file and verify group access headlessly."""

    _configure_logging(verbose)
    cookies, ignored = load_facebook_cookies(auth_file)
    settings = _browser_settings(
        browser=browser,
        profile_dir=profile_dir,
        headless=True,
        executable_path=executable_path,
    )
    group = parse_group_ref(target_id)

    def interrupt_bootstrap(signum: int, frame: object) -> None:
        del signum, frame
        raise BootstrapInterruptedError("Authentication bootstrap was interrupted.")

    previous_sigterm = signal.signal(signal.SIGTERM, interrupt_bootstrap)
    try:
        state = PlaywrightPostSource(settings).bootstrap_auth(
            group,
            cookies,
            navigation_timeout_seconds=navigation_timeout,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    ignored_message = (
        f"; ignored {ignored} non-Facebook cookie{'s' if ignored != 1 else ''}"
        if ignored
        else ""
    )
    click.echo(
        f"Headless authentication bootstrap succeeded for {group.key} "
        f"({state.value}; imported {len(cookies)} Facebook cookies"
        f"{ignored_message})."
    )


@main.command("login")
@_browser_options
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable secret-free operational logging.",
)
@_domain_errors
def login_command(
    *,
    browser: str,
    profile_dir: Path | None,
    executable_path: Path | None,
    verbose: bool,
) -> None:
    """Optionally recover authentication in a headed local browser."""

    _configure_logging(verbose)
    settings = _browser_settings(
        browser=browser,
        profile_dir=profile_dir,
        headless=False,
        executable_path=executable_path,
    )
    click.echo(
        "Opening the optional recovery browser. Complete Facebook login, 2FA, "
        "consent, or checkpoint in that window."
    )

    def wait_for_user() -> None:
        if not click.confirm(
            "Is Facebook login complete in the opened browser?",
            default=False,
        ):
            raise ConfigurationError("Interactive login was cancelled.")

    PlaywrightPostSource(settings).interactive_login(wait_for_user)
    click.echo("Recovery profile saved locally. Cookie values were not exported.")


@main.command("doctor")
@_browser_options
@click.option(
    "--state-file",
    type=click.Path(
        file_okay=True,
        dir_okay=False,
        path_type=Path,
        resolve_path=True,
    ),
    envvar="FBN_STATE_FILE",
    show_envvar=True,
    help="SQLite state file to report.",
)
@_domain_errors
def doctor_command(
    *,
    browser: str,
    profile_dir: Path | None,
    executable_path: Path | None,
    state_file: Path | None,
) -> None:
    """Verify that the selected browser can launch headlessly."""

    settings = _browser_settings(
        browser=browser,
        profile_dir=profile_dir,
        headless=True,
        executable_path=executable_path,
    )
    report = run_doctor(settings, state_file=state_file)
    click.echo(f"fbn version: {__version__}")
    click.echo(f"browser: {report.browser}")
    click.echo(f"profile directory: {report.profile_dir}")
    click.echo(f"state file: {report.state_file}")
    click.echo(f"headless browser: {report.browser_detail}")
    if not report.browser_available:
        raise BrowserUnavailableError(
            "The selected browser failed its local headless launch check."
        )


@main.command("check")
@_browser_options
@_scan_options
@_run_options
@_domain_errors
def check_command(
    *,
    browser: str,
    profile_dir: Path | None,
    executable_path: Path | None,
    sample_count: int,
    max_scrolls: int,
    stagnant_scrolls: int,
    max_post_age: str,
    navigation_timeout: float,
    settle_seconds: float,
    target_id: str,
    state_file: Path | None,
    headless: bool,
    apprise_url: str | None,
    dry_run: bool,
    notify_initial: bool,
    include_errors: bool,
    verbose: bool,
) -> None:
    """Perform one bounded observation and deliver pending posts."""

    _configure_logging(verbose)
    group = parse_group_ref(target_id)
    settings = _browser_settings(
        browser=browser,
        profile_dir=profile_dir,
        headless=headless,
        executable_path=executable_path,
    )
    policy = _scan_policy(
        sample_count=sample_count,
        max_scrolls=max_scrolls,
        stagnant_scrolls=stagnant_scrolls,
        max_post_age=max_post_age,
        navigation_timeout=navigation_timeout,
        settle_seconds=settle_seconds,
    )
    try:
        summary = _run_once(
            group=group,
            settings=settings,
            policy=policy,
            state_file=state_file,
            apprise_url=apprise_url,
            dry_run=dry_run,
            notify_initial=notify_initial,
        )
    except FbnError as exc:
        if include_errors:
            _safe_error_notification(
                apprise_url=apprise_url,
                group=group,
                error=exc,
            )
        raise
    _run_summary(summary)


@main.command("monitor")
@_browser_options
@_scan_options
@_run_options
@click.option(
    "-e",
    "--every",
    help="Minimum interval as a positive integer plus s, m, h, d, or w.",
)
@click.option(
    "-t",
    "--to",
    help="Maximum interval; requires --every and must not be shorter.",
)
@_domain_errors
def monitor_command(
    *,
    browser: str,
    profile_dir: Path | None,
    executable_path: Path | None,
    sample_count: int,
    max_scrolls: int,
    stagnant_scrolls: int,
    max_post_age: str,
    navigation_timeout: float,
    settle_seconds: float,
    target_id: str,
    state_file: Path | None,
    headless: bool,
    apprise_url: str | None,
    dry_run: bool,
    notify_initial: bool,
    include_errors: bool,
    verbose: bool,
    every: str | None,
    to: str | None,
) -> None:
    """Run bounded observations on a persisted, jittered schedule."""

    # Imported lazily so one-shot commands do not initialize scheduling state.
    from .scheduling import MonitorLoop

    _configure_logging(verbose)
    group = parse_group_ref(target_id)
    settings = _browser_settings(
        browser=browser,
        profile_dir=profile_dir,
        headless=headless,
        executable_path=executable_path,
    )
    policy = _scan_policy(
        sample_count=sample_count,
        max_scrolls=max_scrolls,
        stagnant_scrolls=stagnant_scrolls,
        max_post_age=max_post_age,
        navigation_timeout=navigation_timeout,
        settle_seconds=settle_seconds,
    )
    schedule = ScheduleSettings.from_values(every, to)
    sink = _notification_sink(apprise_url=apprise_url, dry_run=dry_run)
    stop_event = threading.Event()

    def stop_monitor(signum: int, frame: object) -> None:
        del frame
        LOGGER.info("Received signal %s; stopping after the active check.", signum)
        stop_event.set()

    previous_handlers: dict[signal.Signals, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, stop_monitor)

    try:
        with SQLiteStateRepository(state_file) as state:
            service = MonitorService(PlaywrightPostSource(settings), state, sink)
            loop = MonitorLoop(service, state, schedule, on_success=_run_summary)
            try:
                loop.run(
                    group,
                    policy,
                    notify_initial=notify_initial,
                    commit_delivery=not dry_run,
                    stop_event=stop_event,
                )
            except FbnError as exc:
                if include_errors:
                    _safe_error_notification(
                        apprise_url=apprise_url,
                        group=group,
                        error=exc,
                    )
                raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    click.echo("monitor stopped")


if __name__ == "__main__":
    main()
