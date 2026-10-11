"""Persistent Playwright acquisition with fail-closed page classification."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from playwright.sync_api import (
    BrowserContext,
    Page,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .config import BrowserSettings
from .exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    BrowserUnavailableError,
    ConfigurationError,
    LayoutChangedError,
    ProfileInUseError,
    TransientNavigationError,
)
from .logging import get_logger
from .models import GroupRef

FACEBOOK_HOME_URL = "https://m.facebook.com/"
LOGGER = get_logger("browser")


class PageState(str, Enum):
    """Recognized safe page states."""

    FEED = "feed"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class PageSignals:
    """Minimal, content-free signals used for page-state classification."""

    url: str
    status: int | None = None
    feed_root_count: int = 0
    feed_item_count: int = 0
    top_level_item_count: int = 0
    direct_permalink_count: int = 0
    story_message_count: int = 0
    primary_body_count: int = 0
    author_count: int = 0
    post_count: int = 0
    has_feed: bool = False
    has_login: bool = False
    has_account_action: bool = False
    has_access_denied: bool = False
    has_rate_limit: bool = False
    has_explicit_empty: bool = False


def _missing_layout_components(signals: PageSignals) -> tuple[str, ...]:
    """Name absent safe structural signals without inspecting page content."""

    missing: list[str] = []
    if not signals.has_feed or signals.feed_root_count == 0:
        missing.append("feed_root")
    if signals.has_feed and signals.feed_item_count == 0:
        missing.append("semantic_feed_item")
    if signals.feed_item_count > 0 and signals.top_level_item_count == 0:
        missing.append("top_level_feed_item")
    if signals.top_level_item_count > 0 and signals.post_count == 0:
        if signals.direct_permalink_count == 0:
            missing.append("direct_post_permalink")
        if signals.story_message_count == 0:
            missing.append("story_message")
        elif signals.primary_body_count == 0:
            missing.append("visible_story_body")
    return tuple(missing or ("recognized_terminal_state",))


def _layout_error_message(signals: PageSignals) -> str:
    missing = ",".join(_missing_layout_components(signals))
    return (
        f"Unrecognized group feed layout: missing={missing}; "
        f"feed_roots={signals.feed_root_count}; "
        f"feed_items={signals.feed_item_count}; "
        f"top_level_items={signals.top_level_item_count}; "
        f"direct_permalinks={signals.direct_permalink_count}; "
        f"story_messages={signals.story_message_count}; "
        f"primary_bodies={signals.primary_body_count}; "
        f"authors={signals.author_count}; "
        f"post_candidates={signals.post_count}."
    )


def classify_page(signals: PageSignals) -> PageState:
    """Classify recognized states or raise a typed fail-closed error."""

    if signals.has_account_action:
        raise AccountActionRequiredError(
            "Facebook requires account action. Refresh the authentication file "
            "or use the optional `fbn login` recovery command."
        )
    if signals.status == 401 or signals.has_login:
        raise AuthenticationRequiredError(
            "The dedicated browser profile is not signed in. Run `fbn bootstrap` "
            "with a fresh authentication file."
        )
    if signals.status == 403:
        raise AccessDeniedError(
            "This profile cannot access the requested Facebook group."
        )
    if signals.status in {404, 410}:
        raise AccessDeniedError(
            "The requested Facebook group is unavailable to this profile."
        )
    if signals.status in {408, 429}:
        raise TransientNavigationError(
            "Facebook returned a temporary response; no retry was attempted."
        )
    if signals.status is not None and signals.status >= 500:
        raise TransientNavigationError(
            f"Facebook returned a temporary HTTP {signals.status} response."
        )
    if signals.post_count and signals.has_feed:
        return PageState.FEED
    if signals.has_explicit_empty and signals.has_feed and signals.feed_item_count == 0:
        return PageState.EMPTY
    # Text phrases are weak signals because a legitimate post can quote them.
    # Consult them only when no recognized feed state is present.
    if signals.has_access_denied:
        raise AccessDeniedError(
            "This profile cannot access the requested Facebook group."
        )
    if signals.has_rate_limit:
        raise TransientNavigationError(
            "Facebook asked this profile to slow down; no retry was attempted."
        )
    raise LayoutChangedError(_layout_error_message(signals))


def _is_visible(page: Page, selector: str) -> bool:
    locator = page.locator(selector)
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightError:
        return False


def _has_explicit_empty_state(page: Page) -> bool:
    if _is_visible(page, '[data-fbn-state="empty"]'):
        return True
    for text in ("No posts yet", "There are no posts"):
        locator = page.get_by_text(text, exact=True)
        try:
            for index in range(min(locator.count(), 20)):
                candidate = locator.nth(index)
                if candidate.is_visible() and candidate.evaluate(
                    """
                    (element) => !element.closest(
                      '[role="article"],[aria-posinset],'
                      + '[data-pagelet*="FeedUnit"]'
                    )
                    """
                ):
                    return True
        except PlaywrightError:
            continue
    return False


def read_page_signals(
    page: Page,
    *,
    status: int | None = None,
    dom_scan_script: str | None = None,
) -> PageSignals:
    """Read only coarse page-state signals; never return page content."""

    parsed = urlparse(page.url)
    path = parsed.path.lower()
    action_path = any(
        marker in path
        for marker in (
            "/checkpoint",
            "/two_step_verification",
            "/consent",
            "/captcha",
        )
    )
    login_path = path.startswith("/login") or path.startswith("/recover")

    main_text = ""
    try:
        main = page.locator('[role="main"]')
        surface = main.first if main.count() else page.locator("body").first
        main_text = surface.inner_text(timeout=1_000)[:50_000].casefold()
    except PlaywrightError:
        pass

    has_rate_limit = any(
        marker in main_text
        for marker in (
            "temporarily blocked",
            "too many requests",
            "try again later",
        )
    )
    has_access_denied = any(
        marker in main_text
        for marker in (
            "content isn't available",
            "content is not available",
            "don't have permission",
            "do not have permission",
        )
    )
    has_explicit_empty = _has_explicit_empty_state(page)

    feed_signals = page.evaluate(dom_scan_script, False) if dom_scan_script else {}
    if not isinstance(feed_signals, dict):
        feed_signals = {}

    return PageSignals(
        url=page.url,
        status=status,
        feed_root_count=int(feed_signals.get("feedRootCount", 0)),
        feed_item_count=int(feed_signals.get("itemCount", 0)),
        top_level_item_count=int(feed_signals.get("containerCount", 0)),
        direct_permalink_count=int(feed_signals.get("directPermalinkCount", 0)),
        story_message_count=int(feed_signals.get("storyMessageCount", 0)),
        primary_body_count=int(feed_signals.get("primaryBodyCount", 0)),
        author_count=int(feed_signals.get("authorCount", 0)),
        post_count=int(feed_signals.get("postCount", 0)),
        has_feed=feed_signals.get("hasFeed") is True,
        has_login=login_path
        or _is_visible(page, 'input[name="email"]')
        or _is_visible(page, 'input[name="pass"]'),
        has_account_action=action_path
        or _is_visible(page, 'input[name="approvals_code"]')
        or _is_visible(page, 'iframe[src*="captcha"]'),
        has_access_denied=has_access_denied,
        has_rate_limit=has_rate_limit,
        has_explicit_empty=has_explicit_empty,
    )


class BrowserSession:
    """Own persistent browser authentication, locking and cleanup."""

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        playwright_factory: Callable[[], Any] = sync_playwright,
    ) -> None:
        self.settings = settings
        self._playwright_factory = playwright_factory

    def interactive_login(self, wait_for_user: Callable[[], None]) -> None:
        """Open a headed profile for optional authentication recovery."""

        LOGGER.info("Interactive login browser opened")
        with self._context(headless=False) as context:
            page: Page | None = None
            try:
                page = context.new_page()
                page.goto(
                    FACEBOOK_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                wait_for_user()
                signals = read_page_signals(page)
                if signals.has_account_action:
                    raise AccountActionRequiredError(
                        "Facebook still requires account action in the open browser."
                    )
                if signals.has_login:
                    raise AuthenticationRequiredError(
                        "Login was not completed in the dedicated browser profile."
                    )
                LOGGER.info("Interactive login verified")
            except PlaywrightTimeoutError as exc:
                raise TransientNavigationError(
                    "Facebook did not finish loading during interactive login."
                ) from exc
            except PlaywrightError as exc:
                raise TransientNavigationError(
                    "Facebook navigation failed during interactive login."
                ) from exc
            finally:
                if page is not None:
                    with suppress(PlaywrightError):
                        page.close()

    def bootstrap_auth(
        self,
        group: GroupRef,
        cookies: Sequence[Mapping[str, object]],
        *,
        navigation_timeout_seconds: float = 60.0,
    ) -> PageState:
        """Import secret-file cookies and verify group access headlessly.

        The existing profile cookies are restored if import or validation fails,
        so a transient bootstrap attempt cannot destroy a working profile.
        """

        profile_dir = self._prepare_profile()
        with self._profile_lock(profile_dir):
            previous_cookies: list[dict[str, object]] | None = None
            mutation_started = False
            imported_cookies = [dict(cookie) for cookie in cookies]
            try:
                LOGGER.debug(
                    "Authentication cookie import started",
                    cookie_count=len(imported_cookies),
                )
                with self._context(headless=True, acquire_lock=False) as context:
                    try:
                        previous_cookies = context.cookies()
                    except PlaywrightError as exc:
                        raise BrowserUnavailableError(
                            "Chromium could not snapshot the existing authentication "
                            "profile."
                        ) from exc
                    mutation_started = True
                    self._set_context_cookies(
                        context,
                        imported_cookies,
                        error_message=(
                            "The authentication file contains cookies Chromium "
                            "could not load."
                        ),
                    )

                # Validation happens in a fresh context while the transaction
                # still owns the one profile lock.
                return self._validate_authenticated_profile(
                    group,
                    navigation_timeout_seconds=navigation_timeout_seconds,
                    acquire_lock=False,
                )
            except BaseException as original:
                if mutation_started and previous_cookies is not None:
                    try:
                        LOGGER.warning(
                            "Authentication bootstrap rollback started",
                            group_key=group.key,
                        )
                        self._replace_profile_cookies(
                            previous_cookies,
                            acquire_lock=False,
                        )
                    except BaseException as rollback_error:
                        if isinstance(
                            rollback_error,
                            (KeyboardInterrupt, SystemExit),
                        ):
                            raise
                        raise ConfigurationError(
                            "Authentication bootstrap failed and rollback of the "
                            "previous profile could not be confirmed."
                        ) from original
                raise

    def _replace_profile_cookies(
        self,
        cookies: Sequence[Mapping[str, object]],
        *,
        acquire_lock: bool = True,
    ) -> None:
        replacement = [dict(cookie) for cookie in cookies]
        with self._context(
            headless=True,
            acquire_lock=acquire_lock,
        ) as context:
            self._set_context_cookies(
                context,
                replacement,
                error_message="Chromium could not restore the previous profile.",
            )

        expected = self._cookie_value_map(replacement)
        with self._context(
            headless=True,
            acquire_lock=acquire_lock,
        ) as context:
            try:
                actual = self._cookie_value_map(context.cookies())
            except PlaywrightError as exc:
                raise ConfigurationError(
                    "Chromium could not verify the restored profile."
                ) from exc
        if actual != expected:
            raise ConfigurationError(
                "Chromium did not persist the complete restored profile."
            )

    @staticmethod
    def _set_context_cookies(
        context: BrowserContext,
        cookies: list[dict[str, object]],
        *,
        error_message: str,
    ) -> None:
        try:
            context.clear_cookies()
            if cookies:
                context.add_cookies(cookies)  # type: ignore[arg-type]
        except PlaywrightError as exc:
            raise ConfigurationError(error_message) from exc

    @staticmethod
    def _cookie_value_map(
        cookies: Sequence[Mapping[str, object]],
    ) -> dict[tuple[str, str, str], object]:
        return {
            (
                str(cookie.get("name", "")),
                str(cookie.get("domain", "")),
                str(cookie.get("path", "/")),
            ): cookie.get("value")
            for cookie in cookies
        }

    def _prepare_profile(self) -> Path:
        profile_dir = Path(self.settings.profile_dir)
        try:
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                profile_dir.chmod(0o700)
        except OSError as exc:
            raise ConfigurationError(
                "The dedicated browser profile could not be prepared."
            ) from exc
        return profile_dir

    @contextmanager
    def _profile_lock(self, profile_dir: Path) -> Iterator[None]:
        lock_path = profile_dir.parent / f".{profile_dir.name}.lock"
        lock = FileLock(str(lock_path))
        try:
            lock.acquire(timeout=0)
        except FileLockTimeout as exc:
            raise ProfileInUseError(
                "The dedicated browser profile is already in use."
            ) from exc

        try:
            LOGGER.debug("Browser profile lock acquired")
            if os.name != "nt" and lock_path.exists():
                lock_path.chmod(0o600)
            yield
        finally:
            lock.release()
            LOGGER.debug("Browser profile lock released")

    @contextmanager
    def _context(
        self,
        *,
        headless: bool,
        acquire_lock: bool = True,
        timezone_id: str | None = None,
    ) -> Iterator[BrowserContext]:
        profile_dir = self._prepare_profile()
        if acquire_lock:
            with (
                self._profile_lock(profile_dir),
                self._open_context(
                    profile_dir,
                    headless=headless,
                    timezone_id=timezone_id,
                ) as context,
            ):
                yield context
            return

        with self._open_context(
            profile_dir,
            headless=headless,
            timezone_id=timezone_id,
        ) as context:
            yield context

    @contextmanager
    def _open_context(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        timezone_id: str | None,
    ) -> Iterator[BrowserContext]:
        marker_missing = self._check_profile_browser(profile_dir)
        stack = ExitStack()
        context: BrowserContext | None = None
        operation_failed = False
        cleanup_error: PlaywrightError | None = None
        try:
            LOGGER.debug(
                "Browser context launching",
                browser=self.settings.browser,
                headless=headless,
                timezone_id=timezone_id or "default",
            )
            try:
                playwright = stack.enter_context(self._playwright_factory())
            except PlaywrightError as exc:
                raise BrowserUnavailableError(
                    "Playwright could not start its browser driver. "
                    "Run `fbn doctor` for installation guidance."
                ) from exc
            context = self._launch_context(
                playwright,
                profile_dir=profile_dir,
                headless=headless,
                timezone_id=timezone_id,
            )
            if marker_missing:
                self._write_profile_browser_marker(profile_dir)
                LOGGER.debug("Browser profile marker written")
            yield context
        except BaseException:
            operation_failed = True
            raise
        finally:
            if context is not None:
                try:
                    context.close()
                except PlaywrightError as exc:
                    cleanup_error = exc
            try:
                stack.close()
            except PlaywrightError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if cleanup_error is not None and not operation_failed:
                raise BrowserUnavailableError(
                    "Playwright could not close its browser session cleanly."
                ) from cleanup_error
            LOGGER.debug("Browser context closed")

    def _browser_identity(self) -> str:
        """Return a stable local identity for profile compatibility."""

        identity = self.settings.browser
        if identity == "executable":
            return f"executable:{self.settings.executable_path}"
        return identity

    def _check_profile_browser(self, profile_dir: Path) -> bool:
        """Reject a profile bound to another browser; report a missing marker."""

        marker = profile_dir / ".fbn-browser"
        try:
            if marker.exists():
                if (
                    marker.read_text(encoding="utf-8").strip()
                    != self._browser_identity()
                ):
                    raise ConfigurationError(
                        "The profile was initialized for a different browser "
                        "configuration. Use a separate profile directory."
                    )
                return False
            return True
        except ConfigurationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(
                "The browser-profile compatibility marker could not be read."
            ) from exc

    def _write_profile_browser_marker(self, profile_dir: Path) -> None:
        """Bind a profile only after its selected browser launches."""

        marker = profile_dir / ".fbn-browser"
        try:
            marker.write_text(f"{self._browser_identity()}\n", encoding="utf-8")
            if os.name != "nt":
                marker.chmod(0o600)
        except (OSError, UnicodeError) as exc:
            with suppress(OSError):
                marker.unlink()
            raise ConfigurationError(
                "The browser-profile compatibility marker could not be written."
            ) from exc

    def _launch_context(
        self,
        playwright: Any,
        *,
        profile_dir: Path,
        headless: bool,
        timezone_id: str | None,
    ) -> BrowserContext:
        kwargs: dict[str, object] = {
            "headless": headless,
            "accept_downloads": False,
        }
        if not headless:
            kwargs["no_viewport"] = True
        if timezone_id is not None:
            kwargs["timezone_id"] = timezone_id

        browser = self.settings.browser
        if browser == "chrome":
            kwargs["channel"] = "chrome"
        elif browser == "msedge":
            kwargs["channel"] = "msedge"
        elif browser == "chromium":
            # Use regular Chromium's current headless implementation. This is
            # supported on Ubuntu ARM64 and also serves headed profile setup.
            kwargs["channel"] = "chromium"
        elif browser == "executable":
            kwargs["executable_path"] = str(self.settings.executable_path)
        else:
            raise BrowserUnavailableError(f"Unsupported browser choice: {browser}.")

        try:
            return playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **kwargs,
            )
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                f"The {browser} browser could not be started. "
                "Use `fbn doctor` to verify the browser installation."
            ) from exc
