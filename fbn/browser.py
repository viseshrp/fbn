"""Persistent Playwright acquisition with fail-closed page classification."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

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
from .extractor import chronological_group_url, extract_posts
from .models import GroupRef, Post, ScanPolicy, ScanResult

FACEBOOK_HOME_URL = "https://www.facebook.com/"
DOM_SCAN_SCRIPT = """
(includeContent) => {
  const linkSelector =
    'a[href*="/groups/"][href*="/posts/"],' +
    'a[href*="/groups/"][href*="/permalink/"]';
  const itemSelector =
    '[role="article"],[aria-posinset],[data-pagelet*="FeedUnit"]';
  const positionedItemSelector =
    '[aria-posinset],[data-pagelet*="FeedUnit"]';
  const feedSelector = '[role="feed"],[data-pagelet*="GroupFeed"]';
  const feedRoots = Array.from(document.querySelectorAll(feedSelector)).filter(
    (root) => root.getClientRects().length > 0
  );
  const cleanContainerText = (container, authorElement) => {
    let text = (container.innerText || '').trim();
    text = text.replace(/^(?:Facebook\\s+){2,}/i, '').trim();

    const author = authorElement
      ? (authorElement.innerText || '').trim()
      : '';
    if (author && text.startsWith(author)) {
      const separator = text.indexOf('·', author.length);
      if (separator >= 0 && separator <= author.length + 512) {
        text = text.slice(separator + 1).trim();
      }
    }
    return text;
  };
  const semanticItems = feedRoots.flatMap(
    (root) => Array.from(root.querySelectorAll(itemSelector))
  ).filter((item) => item.getClientRects().length > 0);
  const anchors = feedRoots.flatMap(
    (root) => Array.from(root.querySelectorAll(linkSelector))
  );
  const seenContainers = new Set();
  const payloads = [];

  for (const anchor of anchors) {
    const feedRoot = anchor.closest(feedSelector);
    let container = null;
    let cursor = anchor;
    while (cursor && cursor !== feedRoot) {
      if (cursor.matches && cursor.matches(itemSelector)) {
        // Select the outer feed item. A quoted/shared post can contain its own
        // nested article and permalink.
        container = cursor;
      }
      cursor = cursor.parentElement;
    }
    if (!container) {
      continue;
    }

    if (container.getClientRects().length === 0) {
      continue;
    }
    if (seenContainers.has(container)) {
      continue;
    }
    seenContainers.add(container);

    const candidates = Array.from(container.querySelectorAll(linkSelector));
    const directCandidates = candidates.filter((candidate) => {
      const positionedItem = candidate.closest(positionedItemSelector);
      if (container.matches(positionedItemSelector)) {
        if (positionedItem !== container) {
          return false;
        }

        // Current Facebook markup places the actual post article inside a
        // separate aria-posinset feed wrapper. Permit that one semantic
        // article layer, but reject deeper quoted/shared post articles.
        let articleDepth = container.matches('[role="article"]') ? 1 : 0;
        let cursor = candidate.parentElement;
        while (cursor && cursor !== container) {
          if (cursor.matches('[role="article"]')) {
            articleDepth += 1;
          }
          cursor = cursor.parentElement;
        }
        return cursor === container && articleDepth <= 1;
      }

      return candidate.closest(itemSelector) === container;
    });
    const selected = directCandidates[0];
    if (!selected) {
      // Never promote the permalink from a nested quoted/shared post to the
      // identity of its outer feed item.
      continue;
    }
    const authorElement = container.querySelector(
      'h2 a, h3 a, h4 a, strong a'
    );
    const collapsed = Boolean(
      container.querySelector('[aria-expanded="false"]')
    );

    payloads.push(
      includeContent
        ? {
            href: selected.href || selected.getAttribute('href') || '',
            text: cleanContainerText(container, authorElement),
            author: authorElement
              ? (authorElement.innerText || '').trim()
              : null,
            partial: collapsed,
            position: payloads.length,
          }
        : null
    );
  }

  return {
    hasFeed: feedRoots.length > 0,
    itemCount: new Set(semanticItems).size,
    postCount: payloads.length,
    payloads: includeContent ? payloads : [],
  };
}
"""


class PageState(str, Enum):
    """Recognized safe page states."""

    FEED = "feed"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class PageSignals:
    """Minimal, content-free signals used for page-state classification."""

    url: str
    status: int | None = None
    feed_item_count: int = 0
    post_count: int = 0
    has_feed: bool = False
    has_login: bool = False
    has_account_action: bool = False
    has_access_denied: bool = False
    has_rate_limit: bool = False
    has_explicit_empty: bool = False


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
    raise LayoutChangedError(
        "The page loaded without a recognized group feed or explicit empty state."
    )


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


def read_page_signals(page: Page, *, status: int | None = None) -> PageSignals:
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

    feed_signals = page.evaluate(DOM_SCAN_SCRIPT, False)
    if not isinstance(feed_signals, dict):
        feed_signals = {}

    return PageSignals(
        url=page.url,
        status=status,
        feed_item_count=int(feed_signals.get("itemCount", 0)),
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


def wait_for_terminal_page(
    page: Page,
    *,
    status: int | None = None,
    timeout_seconds: float,
    expected_group: GroupRef | None = None,
) -> PageState:
    """Poll through dynamic feed skeletons until a recognized state appears."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        signals = read_page_signals(page, status=status)
        try:
            state = classify_page(signals)
        except LayoutChangedError:
            if expected_group is not None and not _is_expected_group_url(
                signals.url,
                expected_group,
            ):
                raise AccessDeniedError(
                    "Facebook redirected away from the requested group."
                ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            page.wait_for_timeout(min(0.25, remaining) * 1_000)
            continue
        if expected_group is not None and not _is_expected_group_url(
            signals.url,
            expected_group,
        ):
            raise AccessDeniedError(
                "Facebook redirected away from the requested group."
            )
        return state


def _is_expected_group_url(url: str, group: GroupRef) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    expected_path = f"/groups/{group.key}".casefold()
    return (
        parsed.scheme.casefold() == "https"
        and (hostname == "facebook.com" or hostname.endswith(".facebook.com"))
        and parsed.path.rstrip("/").casefold() == expected_path
    )


def collect_dom_payloads(page: Page) -> list[dict[str, object]]:
    """Collect minimal visible post payloads from the current DOM."""

    result = page.evaluate(DOM_SCAN_SCRIPT, True)
    if not isinstance(result, dict):
        return []
    payloads = result.get("payloads")
    return payloads if isinstance(payloads, list) else []


def collect_group_aliases(page: Page, group: GroupRef) -> frozenset[str]:
    """Resolve one visible group-navigation alias without trusting page content."""

    discovered: set[str] = set()
    links = page.locator(
        '[role="main"] [role="tablist"] a[role="tab"][href*="/groups/"]'
    )
    try:
        for index in range(min(links.count(), 100)):
            link = links.nth(index)
            if not link.is_visible() or link.evaluate(
                """
                (element) => Boolean(
                  element.closest(
                    '[role="feed"],[data-pagelet*="GroupFeed"]'
                  )
                )
                """
            ):
                continue
            href = link.get_attribute("href")
            if not href:
                continue
            parsed = urlparse(urljoin(page.url, href))
            parts = parsed.path.strip("/").split("/")
            if (
                parsed.hostname
                and (
                    parsed.hostname.casefold() == "facebook.com"
                    or parsed.hostname.casefold().endswith(".facebook.com")
                )
                and len(parts) == 2
                and parts[0].casefold() == "groups"
                and parts[1]
            ):
                discovered.add(parts[1])
    except (PlaywrightError, ValueError):
        return frozenset({group.key})

    if len(discovered) == 1:
        return frozenset({group.key, *discovered})
    return frozenset({group.key})


class PlaywrightPostSource:
    """Fetch visible recent posts through a dedicated persistent browser."""

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

    def _validate_authenticated_profile(
        self,
        group: GroupRef,
        *,
        navigation_timeout_seconds: float,
        acquire_lock: bool = True,
    ) -> PageState:
        with self._context(
            headless=True,
            acquire_lock=acquire_lock,
        ) as context:
            page: Page | None = None
            try:
                page = context.new_page()
                page.set_default_timeout(navigation_timeout_seconds * 1_000)
                page.set_default_navigation_timeout(navigation_timeout_seconds * 1_000)
                response = page.goto(
                    chronological_group_url(group),
                    wait_until="domcontentloaded",
                )
                status = response.status if response is not None else None
                return wait_for_terminal_page(
                    page,
                    status=status,
                    timeout_seconds=navigation_timeout_seconds,
                    expected_group=group,
                )
            except PlaywrightTimeoutError as exc:
                raise TransientNavigationError(
                    "Facebook group validation timed out during authentication "
                    "bootstrap."
                ) from exc
            except PlaywrightError as exc:
                raise TransientNavigationError(
                    "Facebook group navigation failed during authentication bootstrap."
                ) from exc
            finally:
                if page is not None:
                    with suppress(PlaywrightError):
                        page.close()

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

    def fetch_recent(self, group: GroupRef, policy: ScanPolicy) -> ScanResult:
        """Fetch a bounded, deterministic sample from the visible group feed."""

        observed_at = datetime.now(timezone.utc)
        with self._context(headless=self.settings.headless) as context:
            page: Page | None = None
            try:
                page = context.new_page()
                page.set_default_timeout(policy.navigation_timeout_seconds * 1_000)
                page.set_default_navigation_timeout(
                    policy.navigation_timeout_seconds * 1_000
                )
                response = page.goto(
                    chronological_group_url(group),
                    wait_until="domcontentloaded",
                )
                status = response.status if response is not None else None
                state = wait_for_terminal_page(
                    page,
                    status=status,
                    timeout_seconds=policy.navigation_timeout_seconds,
                    expected_group=group,
                )
                if state is PageState.EMPTY:
                    return ScanResult(
                        posts=(),
                        page_state=state.value,
                        scrolls=0,
                        bounded=False,
                    )

                posts = self._scan_feed(page, group, policy, observed_at)
                if not posts:
                    raise LayoutChangedError(
                        "A feed was present, but no supported post permalinks "
                        "were found."
                    )
                return posts
            except PlaywrightTimeoutError as exc:
                raise TransientNavigationError(
                    "Facebook group navigation timed out; no immediate retry "
                    "was attempted."
                ) from exc
            except PlaywrightError as exc:
                raise TransientNavigationError(
                    "Facebook group navigation failed; no immediate retry was "
                    "attempted."
                ) from exc
            finally:
                if page is not None:
                    with suppress(PlaywrightError):
                        page.close()

    def _scan_feed(
        self,
        page: Page,
        group: GroupRef,
        policy: ScanPolicy,
        observed_at: datetime,
    ) -> ScanResult:
        accumulated: list[Post] = []
        seen_ids: set[str] = set()
        stagnant = 0
        scrolls = 0
        allowed_group_keys: frozenset[str] = frozenset({group.key})

        for scan_index in range(policy.max_scrolls + 1):
            state = wait_for_terminal_page(
                page,
                timeout_seconds=policy.navigation_timeout_seconds,
                expected_group=group,
            )
            if state is PageState.EMPTY:
                break
            allowed_group_keys = allowed_group_keys.union(
                collect_group_aliases(page, group)
            )
            before = len(accumulated)
            extracted = extract_posts(
                collect_dom_payloads(page),
                group,
                observed_at,
                limit=policy.sample_count,
                allowed_group_keys=allowed_group_keys,
            )
            for post in extracted:
                if post.post_id in seen_ids:
                    continue
                seen_ids.add(post.post_id)
                accumulated.append(replace(post, position=len(accumulated)))
                if len(accumulated) >= policy.sample_count:
                    return ScanResult(
                        posts=tuple(accumulated),
                        page_state=PageState.FEED.value,
                        scrolls=scrolls,
                        bounded=True,
                    )

            stagnant = stagnant + 1 if len(accumulated) == before else 0
            if stagnant >= policy.stagnant_scrolls or scan_index == policy.max_scrolls:
                break

            page.evaluate(
                "() => window.scrollBy(0, Math.max(window.innerHeight * 0.8, 600))"
            )
            scrolls += 1
            page.wait_for_timeout(policy.settle_seconds * 1_000)

        return ScanResult(
            posts=tuple(accumulated),
            page_state=PageState.FEED.value,
            scrolls=scrolls,
            bounded=(
                scrolls >= policy.max_scrolls or stagnant >= policy.stagnant_scrolls
            ),
        )

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
            if os.name != "nt" and lock_path.exists():
                lock_path.chmod(0o600)
            yield
        finally:
            lock.release()

    @contextmanager
    def _context(
        self,
        *,
        headless: bool,
        acquire_lock: bool = True,
    ) -> Iterator[BrowserContext]:
        profile_dir = self._prepare_profile()
        if acquire_lock:
            with (
                self._profile_lock(profile_dir),
                self._open_context(
                    profile_dir,
                    headless=headless,
                ) as context,
            ):
                yield context
            return

        with self._open_context(profile_dir, headless=headless) as context:
            yield context

    @contextmanager
    def _open_context(
        self,
        profile_dir: Path,
        *,
        headless: bool,
    ) -> Iterator[BrowserContext]:
        marker_missing = self._check_profile_browser(profile_dir)
        stack = ExitStack()
        context: BrowserContext | None = None
        operation_failed = False
        cleanup_error: PlaywrightError | None = None
        try:
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
            )
            if marker_missing:
                self._write_profile_browser_marker(profile_dir)
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
    ) -> BrowserContext:
        kwargs: dict[str, object] = {
            "headless": headless,
            "accept_downloads": False,
        }
        if not headless:
            kwargs["no_viewport"] = True

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
