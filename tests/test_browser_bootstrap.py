from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

import fbn.browser as browser_module
from fbn.browser import PageSignals, PageState, PlaywrightPostSource
from fbn.config import BrowserSettings
from fbn.exceptions import (
    AuthenticationRequiredError,
    BrowserUnavailableError,
    ConfigurationError,
    TransientNavigationError,
)
from fbn.extractor import parse_group_ref
from fbn.models import ScanPolicy


class FakeLocator:
    @property
    def first(self) -> FakeLocator:
        return self

    def wait_for(self, **kwargs: object) -> None:
        del kwargs


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False
        self.url = "https://www.facebook.com/groups/example/"

    def set_default_timeout(self, timeout: float) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: float) -> None:
        del timeout

    def goto(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        self.url = url
        if self.error is not None:
            raise self.error
        return FakeResponse()

    def locator(self, selector: str) -> FakeLocator:
        del selector
        return FakeLocator()

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(
        self,
        previous: Sequence[dict[str, object]],
        *,
        page_error: BaseException | None = None,
        new_page_error: BaseException | None = None,
        cookie_error_reads: set[int] | None = None,
    ) -> None:
        self.current = list(previous)
        self.added: list[list[dict[str, object]]] = []
        self.clear_count = 0
        self.page = FakePage(page_error)
        self.new_page_error = new_page_error
        self.requested_headless: list[bool] = []
        self.requested_lock: list[bool] = []
        self.requested_timezone: list[str | None] = []
        self.cookie_reads = 0
        self.cookie_error_reads = cookie_error_reads or set()
        self.closed = False

    def cookies(self) -> list[dict[str, object]]:
        self.cookie_reads += 1
        if self.cookie_reads in self.cookie_error_reads:
            raise PlaywrightError("private-cookie-snapshot-detail")
        return list(self.current)

    def clear_cookies(self) -> None:
        self.clear_count += 1
        self.current = []

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        copied = list(cookies)
        self.added.append(copied)
        self.current = copied

    def new_page(self) -> FakePage:
        if self.new_page_error is not None:
            raise self.new_page_error
        return self.page

    def close(self) -> None:
        self.closed = True


def source_with_context(
    tmp_path: Path,
    context: FakeContext,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings_headless: bool = False,
) -> PlaywrightPostSource:
    source = PlaywrightPostSource(
        BrowserSettings(
            browser="chromium",
            profile_dir=tmp_path / "profile",
            headless=settings_headless,
        )
    )

    @contextmanager
    def fake_context(
        *,
        headless: bool,
        acquire_lock: bool = True,
        timezone_id: str | None = None,
    ) -> Iterator[FakeContext]:
        context.requested_headless.append(headless)
        context.requested_lock.append(acquire_lock)
        context.requested_timezone.append(timezone_id)
        yield context

    monkeypatch.setattr(source, "_context", fake_context)
    return source


def test_bootstrap_replaces_profile_cookies_and_validates_headlessly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = [{"name": "old", "value": "old-secret"}]
    imported = [
        {
            "name": "new",
            "value": "new-secret",
            "domain": ".facebook.com",
            "path": "/",
        }
    ]
    context = FakeContext(previous)
    source = source_with_context(tmp_path, context, monkeypatch)
    monkeypatch.setattr(
        browser_module,
        "read_page_signals",
        lambda page, *, status=None: PageSignals(
            url=page.url,
            status=status,
            post_count=1,
            has_feed=True,
        ),
    )

    state = source.bootstrap_auth(parse_group_ref("example"), imported)

    assert state is PageState.FEED
    assert context.current == imported
    assert context.added == [imported]
    assert context.clear_count == 1
    assert context.page.closed is True
    assert context.requested_headless == [True, True]
    assert context.requested_lock == [False, False]


def test_bootstrap_holds_one_profile_lock_across_import_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext(())
    source = source_with_context(tmp_path, context, monkeypatch)
    active = False
    events: list[str] = []
    original_context = source._context

    @contextmanager
    def fake_profile_lock(profile_dir: Path) -> Iterator[None]:
        nonlocal active
        assert profile_dir == source.settings.profile_dir
        assert active is False
        active = True
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")
            active = False

    @contextmanager
    def guarded_context(**kwargs: object) -> Iterator[FakeContext]:
        assert active is True
        with original_context(**kwargs) as opened:  # type: ignore[arg-type]
            yield opened

    monkeypatch.setattr(source, "_profile_lock", fake_profile_lock)
    monkeypatch.setattr(source, "_context", guarded_context)
    monkeypatch.setattr(
        browser_module,
        "read_page_signals",
        lambda page, *, status=None: PageSignals(
            url=page.url,
            status=status,
            post_count=1,
            has_feed=True,
        ),
    )

    source.bootstrap_auth(
        parse_group_ref("example"),
        [
            {
                "name": "new",
                "value": "new-secret",
                "domain": ".facebook.com",
                "path": "/",
            }
        ],
    )

    assert events == ["acquire", "release"]
    assert context.requested_lock == [False, False]


def test_bootstrap_restores_previous_cookies_when_auth_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = [{"name": "old", "value": "old-secret"}]
    imported = [
        {
            "name": "new",
            "value": "new-secret",
            "domain": ".facebook.com",
            "path": "/",
        }
    ]
    context = FakeContext(previous)
    source = source_with_context(tmp_path, context, monkeypatch)
    monkeypatch.setattr(
        browser_module,
        "read_page_signals",
        lambda page, *, status=None: PageSignals(
            url=page.url,
            status=status,
            has_login=True,
        ),
    )

    with pytest.raises(AuthenticationRequiredError):
        source.bootstrap_auth(parse_group_ref("example"), imported)

    assert context.current == previous
    assert context.added == [imported, previous]
    assert context.clear_count == 2
    assert context.page.closed is True
    assert context.requested_headless == [True, True, True, True]


def test_bootstrap_redacts_playwright_navigation_error_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "browser-error-containing-cookie-value"
    previous = [{"name": "old", "value": "old-secret"}]
    imported = [
        {
            "name": "new",
            "value": "new-secret",
            "domain": ".facebook.com",
            "path": "/",
        }
    ]
    context = FakeContext(previous, page_error=PlaywrightError(secret))
    source = source_with_context(tmp_path, context, monkeypatch)

    with pytest.raises(TransientNavigationError) as caught:
        source.bootstrap_auth(parse_group_ref("example"), imported)

    assert secret not in str(caught.value)
    assert context.current == previous
    assert context.page.closed is True


def test_bootstrap_rolls_back_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = [{"name": "old", "value": "old-secret"}]
    imported = [
        {
            "name": "new",
            "value": "new-secret",
            "domain": ".facebook.com",
            "path": "/",
        }
    ]
    context = FakeContext(previous, page_error=KeyboardInterrupt())
    source = source_with_context(tmp_path, context, monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        source.bootstrap_auth(parse_group_ref("example"), imported)

    assert context.current == previous
    assert context.added == [imported, previous]


def test_bootstrap_maps_cookie_snapshot_failure_without_mutating_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = [{"name": "old", "value": "old-secret"}]
    context = FakeContext(previous, cookie_error_reads={1})
    source = source_with_context(tmp_path, context, monkeypatch)

    with pytest.raises(BrowserUnavailableError) as caught:
        source.bootstrap_auth(
            parse_group_ref("example"),
            [
                {
                    "name": "new",
                    "value": "new-secret",
                    "domain": ".facebook.com",
                    "path": "/",
                }
            ],
        )

    assert "private-cookie-snapshot-detail" not in str(caught.value)
    assert context.current == previous
    assert context.clear_count == 0


def test_fetch_maps_generic_playwright_navigation_error_to_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-playwright-network-detail"
    context = FakeContext((), page_error=PlaywrightError(secret))
    source = source_with_context(
        tmp_path,
        context,
        monkeypatch,
        settings_headless=True,
    )

    with pytest.raises(TransientNavigationError) as caught:
        source.fetch_recent(parse_group_ref("example"), ScanPolicy())

    assert secret not in str(caught.value)
    assert context.page.closed is True
    assert context.requested_headless == [True]
    assert context.requested_lock == [True]
    assert context.requested_timezone == ["UTC"]


@pytest.mark.parametrize("operation", ["bootstrap", "fetch", "login"])
def test_page_creation_error_is_redacted_for_every_browser_operation(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-new-page-driver-detail"
    previous = [{"name": "old", "value": "old-secret"}]
    context = FakeContext(
        previous,
        new_page_error=PlaywrightError(secret),
    )
    source = source_with_context(
        tmp_path,
        context,
        monkeypatch,
        settings_headless=True,
    )

    with pytest.raises(TransientNavigationError) as caught:
        if operation == "bootstrap":
            source.bootstrap_auth(
                parse_group_ref("example"),
                [
                    {
                        "name": "new",
                        "value": "new-secret",
                        "domain": ".facebook.com",
                        "path": "/",
                    }
                ],
            )
        elif operation == "fetch":
            source.fetch_recent(parse_group_ref("example"), ScanPolicy())
        else:
            source.interactive_login(lambda: None)

    assert secret not in str(caught.value)
    if operation == "bootstrap":
        assert context.current == previous


class FakePersistentChromium:
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    def launch_persistent_context(
        self,
        profile_dir: str,
        **kwargs: object,
    ) -> FakeContext:
        del profile_dir, kwargs
        return self.context


class TeardownErrorManager:
    def __init__(self, context: FakeContext, secret: str) -> None:
        self.chromium = FakePersistentChromium(context)
        self.secret = secret

    def __enter__(self) -> TeardownErrorManager:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        raise PlaywrightError(self.secret)


class NormalManager:
    def __init__(self, chromium: object) -> None:
        self.chromium = chromium

    def __enter__(self) -> NormalManager:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class FailingPersistentChromium:
    def launch_persistent_context(
        self,
        profile_dir: str,
        **kwargs: object,
    ) -> FakeContext:
        del profile_dir, kwargs
        raise PlaywrightError("missing-browser-private-detail")


def test_context_maps_driver_teardown_error_without_raw_details(
    tmp_path: Path,
) -> None:
    secret = "private-driver-shutdown-detail"
    context = FakeContext(())
    manager = TeardownErrorManager(context, secret)
    source = PlaywrightPostSource(
        BrowserSettings(profile_dir=tmp_path / "profile"),
        playwright_factory=lambda: manager,
    )

    with (
        pytest.raises(BrowserUnavailableError) as caught,
        source._context(headless=True),
    ):
        pass

    assert secret not in str(caught.value)


def test_profile_rejects_a_different_browser_configuration(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".fbn-browser").write_text("chrome\n", encoding="utf-8")
    source = PlaywrightPostSource(
        BrowserSettings(browser="chromium", profile_dir=profile),
    )

    with (
        pytest.raises(ConfigurationError, match="different browser"),
        source._context(headless=True),
    ):
        pass


def test_profile_marker_includes_explicit_executable_path(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    executable = tmp_path / "chromium"
    context = FakeContext(())
    source = PlaywrightPostSource(
        BrowserSettings(
            browser="executable",
            profile_dir=profile,
            executable_path=executable,
        ),
        playwright_factory=lambda: NormalManager(FakePersistentChromium(context)),
    )

    with source._context(headless=True):
        pass

    assert (profile / ".fbn-browser").read_text(encoding="utf-8") == (
        f"executable:{executable}\n"
    )


def test_failed_first_browser_launch_does_not_bind_empty_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    failing = PlaywrightPostSource(
        BrowserSettings(browser="chromium", profile_dir=profile),
        playwright_factory=lambda: NormalManager(FailingPersistentChromium()),
    )

    with (
        pytest.raises(BrowserUnavailableError),
        failing._context(headless=True),
    ):
        pass

    assert not (profile / ".fbn-browser").exists()

    context = FakeContext(())
    corrected = PlaywrightPostSource(
        BrowserSettings(browser="chrome", profile_dir=profile),
        playwright_factory=lambda: NormalManager(FakePersistentChromium(context)),
    )
    with corrected._context(headless=True):
        pass

    assert (profile / ".fbn-browser").read_text(encoding="utf-8") == "chrome\n"
