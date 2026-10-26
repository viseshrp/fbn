"""Mobile Facebook acquisition using verified primary-post Copy link actions."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, ElementHandle, Page, Response, Route
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    BrowserSession,
    PageSignals,
    PageState,
    classify_page,
    read_page_signals,
)
from .config import MOBILE_SCAN_BUDGET_SECONDS
from .exceptions import (
    AccessDeniedError,
    BrowserUnavailableError,
    ConfigurationError,
    LayoutChangedError,
    RenderTimeoutError,
    TransientNavigationError,
)
from .extractor import (
    FACEBOOK_HOSTS,
    PostLink,
    extract_posts,
    parse_facebook_timestamp,
    parse_group_ref,
    parse_post_url,
)
from .failure_screenshots import capture_failure_screenshots
from .logging import get_logger
from .mobile_compare import comparison_script
from .mobile_dom import (
    APP_PROMPT_SCRIPT,
    MOBILE_PERMALINK_SCAN_SCRIPT,
    MOBILE_SCAN_SCRIPT,
    RENDER_SNAPSHOT_SCRIPT,
    PostView,
)
from .mobile_view import post_view_issues, preview_matches_expanded
from .models import GroupRef, Post, ScanPolicy, ScanResult

LOGGER = get_logger("mobile")
MAX_SCAN_SECONDS = MOBILE_SCAN_BUDGET_SECONDS
COPY_TIMEOUT_SECONDS = 30.0
MAX_LINK_SECONDS = 30.0
MAX_EXPANSION_SECONDS = 15.0

_SCAN_DEADLINE: ContextVar[float | None] = ContextVar("fbn_scan_deadline", default=None)


class _ScanBudgetExpired(Exception):
    """Return only completed posts; this is not a changed or accepted layout."""


def _annotate_failure(error: Exception, phase: str) -> None:
    if not hasattr(error, "_fbn_failure_phase"):
        error._fbn_failure_phase = phase
    deadline = _SCAN_DEADLINE.get()
    if deadline is not None:
        error._fbn_remaining_scan_ms = max(
            0, min(90_000, int((deadline - time.monotonic()) * 1_000))
        )


def _check_scan_budget() -> None:
    deadline = _SCAN_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise _ScanBudgetExpired


_SORT_CONTROL_SCRIPT = r"""
({label, feedOnly = false}) => {
  const clean = value => (value || '').normalize('NFKC')
    .replace(/[\u034f\u200b-\u200f\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll(
    '[data-mcomponent="ServerTextArea"],[data-mcomponent="TextArea"]'
  )).filter(node => node.getClientRects().length
    && clean(node.innerText).toLowerCase() === label.toLowerCase()
    && !node.closest('[role="article"],[aria-label^="Comment by "]'));
  // Sort text belongs to a group control, not to a story body.
  const controls = nodes.filter(node => {
    const root = node.closest('[data-type="vscroller"]');
    if (!root) return !feedOnly;  // Sort menu lives outside the feed scroller.
    let section = node;
    while (section.parentElement && section.parentElement !== root) {
      section = section.parentElement;
    }
    return !section.querySelector('[aria-label^="More options for "]');
  });
  return controls.length === 1 ? controls[0] : null;
}
"""

_CAPTURE_INSTALL_SCRIPT = r"""
() => {
  if (window.__fbnCopyCapture) throw new Error('capture already installed');
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  const original = navigator.clipboard;
  const capture = {descriptor, values: []};
  const replacement = Object.create(original || null);
  Object.defineProperty(replacement, 'writeText', {
    value: async value => { capture.values.push(String(value)); },
  });
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: replacement,
  });
  window.__fbnCopyCapture = capture;
}
"""
_CAPTURE_RESTORE_SCRIPT = """
() => {
  const capture = window.__fbnCopyCapture;
  if (!capture) return;
  if (capture.descriptor) {
    Object.defineProperty(navigator, 'clipboard', capture.descriptor);
  } else {
    delete navigator.clipboard;
  }
  delete window.__fbnCopyCapture;
}
"""


def mobile_group_url(group: GroupRef) -> str:
    key = parse_group_ref(group.key).key
    return f"https://m.facebook.com/groups/{key}/?sorting_setting=CHRONOLOGICAL"


def _trusted_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in FACEBOOK_HOSTS
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _group_key(value: str) -> str | None:
    if not _trusted_url(value):
        return None
    try:
        return parse_group_ref(value).key
    except (ValueError, ConfigurationError):
        return None


class InitialGroupNavigation:
    """Browser-observed provenance, collected only before sort/menu actions.

    A history rename must belong to the original successful group document.
    Child frames, later documents and aliases scraped from content cannot prove it.
    """

    def __init__(self, frame_id: str) -> None:
        self.frame_id = frame_id
        self.events: list[tuple[str, str]] = []
        self.overflow = False

    def _append(self, kind: str, url: str) -> None:
        if len(self.events) >= 10:
            self.overflow = True
        else:
            self.events.append((kind, url))

    def document_event(self, event: dict[str, Any]) -> None:
        frame = event["frame"]
        if frame["id"] == self.frame_id:
            self._append("document", frame["url"])

    def history_event(self, event: dict[str, Any]) -> None:
        if event["frameId"] == self.frame_id:
            self._append(event.get("navigationType", "unknown"), event["url"])

    def proves_alias(self, response: Response, group: GroupRef, final: str) -> bool:
        final_key = _group_key(final)
        return bool(
            not self.overflow
            and group.key.isdigit()
            and final_key
            and not final_key.isdigit()
            and response.status == 200
            and _group_key(response.url) == group.key
            and response.request.redirected_from is None
            and len(self.events) >= 2
            and self.events[0] == ("document", response.url)
            and self.events[-1] == ("historyApi", final)
            and all(
                kind == "historyApi" and _group_key(url) in {group.key, final_key}
                for kind, url in self.events[1:]
            )
        )


@contextmanager
def initial_group_navigation(page: Page) -> Iterator[InitialGroupNavigation]:
    session = page.context.new_cdp_session(page)
    try:
        session.send("Page.enable")
        frame_id = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        proof = InitialGroupNavigation(frame_id)
        session.on("Page.frameNavigated", proof.document_event)
        session.on("Page.navigatedWithinDocument", proof.history_event)
        yield proof
    finally:
        # Do not let a subsequent sort, post menu or navigation bless an alias.
        with suppress(PlaywrightError):
            session.detach()


def verified_group_keys(
    response: Response | None,
    group: GroupRef,
    final_url: str,
    *,
    navigation: InitialGroupNavigation | None = None,
) -> frozenset[str]:
    """Trust HTTP redirects or the initial group document's history rename."""

    final_key = _group_key(final_url)
    if final_key == group.key:
        return frozenset({group.key})
    if response is None or final_key is None or not group.key.isdigit():
        raise AccessDeniedError("Mobile navigation left the verified group.")
    if navigation is not None and navigation.proves_alias(response, group, final_url):
        return frozenset({group.key, final_key})
    chain = []
    request = response.request
    while request is not None and len(chain) < 10:
        chain.append(_group_key(request.url))
        request = request.redirected_from
    if (
        request is not None
        or len(chain) < 2
        or chain[-1] != group.key
        or chain[0] != final_key
        or any(key not in {group.key, final_key} for key in chain)
    ):
        raise AccessDeniedError("Mobile group alias provenance could not be verified.")
    return frozenset({group.key, final_key})


def _layout_failure(*missing: str, phase: str = "layout") -> LayoutChangedError:
    # All component names are constants, never server content or selectors.
    components = ",".join(sorted(set(missing)))
    LOGGER.warning("Mobile layout incomplete", missing_components=components)
    error = LayoutChangedError(f"Mobile layout incomplete: missing={components}.")
    _annotate_failure(error, phase)
    return error


def _remaining_seconds(deadline: float, phase: str) -> float:
    _check_scan_budget()
    scan_deadline = _SCAN_DEADLINE.get()
    if scan_deadline is not None:
        deadline = min(deadline, scan_deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        LOGGER.warning("Mobile action timed out", phase=phase)
        error = TransientNavigationError(
            f"Mobile action timed out: phase={phase}; no immediate retry was attempted."
        )
        _annotate_failure(error, phase)
        raise error
    return remaining


@contextmanager
def _action_phase(phase: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    except PlaywrightError as error:
        _annotate_failure(error, phase)
        _check_scan_budget()
        LOGGER.warning(
            "Mobile browser action failed",
            phase=phase,
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        raise
    except Exception as error:
        _annotate_failure(error, phase)
        raise


def _dismiss_app_prompt(page: Page, deadline: float) -> bool:
    handle = page.evaluate_handle(APP_PROMPT_SCRIPT)
    try:
        control = handle.as_element()
        if control is None:
            return False
        # A known app prompt must never bless a login/checkpoint or a different
        # navigation. Only this one non-post action is eligible for dismissal.
        signals = read_page_signals(page, dom_scan_script=MOBILE_SCAN_SCRIPT)
        with suppress(LayoutChangedError):
            classify_page(signals)
        if getattr(page, "_fbn_app_prompt_dismissed", False):
            raise _layout_failure("single_app_prompt_dismissal", phase="app_prompt")
        original = page.url
        with _action_phase("dismiss_app_prompt"):
            control.click(
                timeout=_remaining_seconds(deadline, "dismiss_app_prompt") * 1_000
            )
            page.wait_for_function(
                f"() => ({APP_PROMPT_SCRIPT})() === null",
                timeout=_remaining_seconds(deadline, "dismiss_app_prompt") * 1_000,
            )
        if page.url != original:
            raise _layout_failure("unchanged_app_prompt_navigation", phase="app_prompt")
        page._fbn_app_prompt_dismissed = True
        LOGGER.info("Dismissed recognized app-install prompt")
        return True
    finally:
        handle.dispose()


def _log_layout_snapshot(
    page: Page,
    result: dict[str, Any],
    *,
    permalink: bool,
    phase: str = "page_render",
    verification_post_count: int | None = None,
    signals: PageSignals | None = None,
) -> dict[str, Any]:
    # Fixed structural counters only. Never log text, labels, URLs, or IDs.
    structure = page.evaluate(RENDER_SNAPSHOT_SCRIPT)
    terminal_signals = (
        {
            name: getattr(signals, name)
            for name in (
                "has_login",
                "has_account_action",
                "has_access_denied",
                "has_rate_limit",
                "has_explicit_empty",
            )
        }
        if signals is not None
        else {}
    )
    LOGGER.warning(
        "Mobile layout snapshot",
        page_kind="permalink" if permalink else "feed",
        phase=phase,
        verification_post_count=verification_post_count,
        missing_components=",".join(_missing_mobile_components(result)),
        visible_scrollers=result.get("feedRootCount", 0),
        primary_candidates=result.get("itemCount", 0),
        valid_posts=result.get("postCount", 0),
        retryable_header_cards=result.get("headerAssemblyCount", 0),
        unclassified_menu_cards=result.get("unclassifiedMenuCards", 0),
        compound_wrappers=result.get("compoundCardCount", 0),
        **terminal_signals,
        **structure,
    )
    return structure


def _is_loading_shell(result: dict[str, Any], structure: dict[str, Any]) -> bool:
    """Recognize a blank, non-interactive shell, not arbitrary missing selectors.

    Some Facebook boot spinners have neither a role nor mobile components.
    A progressbar alone cannot excuse content, a changed card, or an unknown UI.
    This classification permits only delayed retry, never accepting an empty feed.
    """

    return all(
        value == 0
        for value in (
            result.get("feedRootCount"),
            result.get("itemCount"),
            result.get("postCount"),
            structure.get("total_scrollers"),
            structure.get("primary_menus"),
            structure.get("dialogs"),
            structure.get("visible_text_chars"),
            structure.get("visible_controls"),
            structure.get("visible_media"),
        )
    )


def _raise_render_timeout(
    phase: str = "page_render",
    *,
    loading_shell: bool = True,
    header_assembly: bool = False,
) -> None:
    # The scheduler owns bounded seconds-scale retries within this check.
    # Do not reload, reopen the link, or reset either action/scan deadline here.
    surface = (
        "recognized header assembly"
        if header_assembly
        else "blank loading shell"
        if loading_shell
        else "supported page"
    )
    error = RenderTimeoutError(
        f"Mobile page rendering timed out on a {surface}; "
        "no posts were accepted and no immediate retry was attempted."
    )
    _annotate_failure(error, phase)
    error._fbn_render_classified = True
    raise error


def _is_header_assembly(result: dict[str, Any]) -> bool:
    """Known metadata/body/footer evidence permits recovery, never acceptance."""
    names = (
        "feedRootCount",
        "itemCount",
        "postCount",
        "headerAssemblyCount",
        "unclassifiedMenuCards",
    )
    if any(type(result.get(name)) is not int or result[name] < 0 for name in names):
        return False
    return (
        result.get("missing") == ["unique_primary_menu"]
        and result["feedRootCount"] == 1
        and result["headerAssemblyCount"] > 0
        and result["unclassifiedMenuCards"] == 0
        and result["itemCount"] == result["postCount"] + result["headerAssemblyCount"]
    )


def _missing_mobile_components(result: dict[str, Any]) -> tuple[str, ...]:
    missing = result.get("missing", [])
    if not missing and result.get("postCount", 0) == 0:
        missing = ["primary_mobile_card"]
    return tuple(sorted(set(missing)))


def _inspect_failed_render(
    page: Page,
    *,
    permalink: bool,
    phase: str,
    verification_post_count: int | None = None,
    expected_url: str | None = None,
) -> dict[str, Any]:
    """Classify current evidence without retrying, accepting, or adding a wait.

    All render-dependent paths use the same security/loading distinction.
    A blank shell aborts this scan; the scheduler alone owns delayed retries.
    Visible unknown layouts and identity changes never earn transient status.
    """

    script = MOBILE_PERMALINK_SCAN_SCRIPT if permalink else MOBILE_SCAN_SCRIPT
    signals = read_page_signals(page, dom_scan_script=script)
    result = page.evaluate(script, False)
    structure = _log_layout_snapshot(
        page,
        result,
        permalink=permalink,
        phase=phase,
        verification_post_count=verification_post_count,
        signals=signals,
    )
    try:
        with suppress(LayoutChangedError):
            classify_page(signals)
        if expected_url is not None and page.url != expected_url:
            raise _layout_failure("expanded_primary_url_unchanged", phase=phase)
        if _is_loading_shell(result, structure):
            _raise_render_timeout(phase)
        if _is_header_assembly(result):
            _raise_render_timeout(phase, loading_shell=False, header_assembly=True)
    except Exception as error:
        _annotate_failure(error, phase)
        error._fbn_render_classified = True
        raise
    return result


def wait_for_mobile_page(
    page: Page,
    *,
    timeout_seconds: float,
    status: int | None = None,
    permalink: bool = False,
) -> PageState:
    script = MOBILE_PERMALINK_SCAN_SCRIPT if permalink else MOBILE_SCAN_SCRIPT
    deadline = time.monotonic() + timeout_seconds
    while True:
        _check_scan_budget()
        signals = read_page_signals(page, status=status, dom_scan_script=script)
        if signals.has_login or signals.has_account_action:
            classify_page(signals)
        if _dismiss_app_prompt(page, deadline):
            continue
        if page.locator('[role="dialog"], [aria-modal="true"]').evaluate_all(
            "nodes => nodes.some(node => node.checkVisibility({"
            "visibilityProperty:true,opacityProperty:true}))"
        ):
            raise _layout_failure("supported_blocking_dialog", phase="page_render")
        state = None
        with suppress(LayoutChangedError):
            state = classify_page(signals)
        result = page.evaluate(script, False)
        if state is not None and not result.get("missing"):
            return state
        if time.monotonic() >= deadline:
            result = _inspect_failed_render(
                page, permalink=permalink, phase="page_render"
            )
            if not result.get("missing") and result.get("postCount"):
                # The view may finish rendering between the coarse state read
                # and this final snapshot. Abort rather than accept it using an
                # earlier state or report a layout failure with nothing missing.
                _raise_render_timeout("page_render", loading_shell=False)
            raise _layout_failure(
                *_missing_mobile_components(result), phase="page_render"
            )
        page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1_000)))


def _control(
    page: Page, label: str, *, feed_only: bool = False
) -> ElementHandle | None:
    handle = page.evaluate_handle(
        _SORT_CONTROL_SCRIPT, {"label": label, "feedOnly": feed_only}
    )
    element = handle.as_element()
    if element is None:
        handle.dispose()
    return element


def select_new_posts(page: Page, *, timeout_seconds: float) -> None:
    """Select and verify the visible sort, even if the URL asks for chronology."""

    deadline = time.monotonic() + timeout_seconds
    page.bring_to_front()
    _dismiss_app_prompt(page, deadline)
    handle = _control(page, "New posts", feed_only=True)
    if handle is not None:
        handle.dispose()
        return
    sort = _control(page, "Sort")
    if sort is None:
        _inspect_failed_render(page, permalink=False, phase="open_sort")
        raise _layout_failure("group_sort_control")
    try:
        with _action_phase("open_sort"):
            sort.click(timeout=_remaining_seconds(deadline, "open_sort") * 1_000)
    finally:
        sort.dispose()
    new_posts = None
    while new_posts is None and time.monotonic() < deadline:
        new_posts = _control(page, "New posts")
        if new_posts is None:
            page.wait_for_timeout(100)
    if new_posts is None:
        _inspect_failed_render(page, permalink=False, phase="select_new_posts")
        raise _layout_failure("new_posts_sort_choice")
    try:
        with _action_phase("select_new_posts"):
            new_posts.click(
                timeout=_remaining_seconds(deadline, "select_new_posts") * 1_000
            )
    finally:
        new_posts.dispose()
    with _action_phase("verify_new_posts"):
        page.wait_for_function(
            f"() => ({_SORT_CONTROL_SCRIPT})"
            "({label: 'New posts', feedOnly: true}) !== null",
            timeout=_remaining_seconds(deadline, "verify_new_posts") * 1_000,
        )
    LOGGER.debug("Mobile feed sort verified", sort="new_posts")


@contextmanager
def mobile_records(
    page: Page, *, permalink: bool = False
) -> Iterator[list[tuple[ElementHandle, PostView]]]:
    script = MOBILE_PERMALINK_SCAN_SCRIPT if permalink else MOBILE_SCAN_SCRIPT
    root = page.evaluate_handle(comparison_script(script, permalink=permalink), True)
    handles = []
    try:
        comparison = root.get_property("comparison")
        handles.append(comparison)
        comparison_value = comparison.json_value()
        if comparison_value is not None:
            LOGGER.info(
                "Mobile parser comparison",
                page_kind="permalink" if permalink else "feed",
                **comparison_value,
            )
            if comparison_value.get("matched") is not True:
                raise _layout_failure(
                    "reference_parser_agreement", phase="parser_comparison"
                )
        result = root.get_property("records")
        handles.append(result)
        records = []
        for key, record in result.get_properties().items():
            handles.append(record)
            if not key.isdigit():
                continue
            menu_handle = record.get_property("menu")
            payload_handle = record.get_property("payload")
            handles.extend([menu_handle, payload_handle])
            menu = menu_handle.as_element()
            if menu is not None:
                payload = payload_handle.json_value()
                issues = post_view_issues(payload)
                if issues:
                    raise _layout_failure(*issues, phase="post_view_validation")
                records.append((menu, payload))
        yield records
    finally:
        for handle in reversed(handles):
            with suppress(PlaywrightError):
                handle.dispose()
        with suppress(PlaywrightError):
            root.dispose()


def expand_post_preview(
    page: Page,
    menu: ElementHandle,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    permalink: bool = False,
) -> dict[str, Any]:
    """Expand this card, then require the same owner, date, and preview prefix."""

    if not payload.get("partial"):
        return payload
    if payload.get("previewControlCount") != 1:
        raise _layout_failure("unique_primary_preview_control")
    deadline = time.monotonic() + timeout_seconds
    script = MOBILE_PERMALINK_SCAN_SCRIPT if permalink else MOBILE_SCAN_SCRIPT
    original_url = page.url
    expansion = None
    completed = None
    phase = "find_primary_preview"
    try:
        with _action_phase(phase):
            page.bring_to_front()
            _dismiss_app_prompt(page, deadline)
            menu.evaluate(
                "node => node.scrollIntoView({block:'center',inline:'nearest'})"
            )
            expansion = page.wait_for_function(
                f"menu => ({script})(true).records"
                ".find(record => record.menu === menu)?.expand || null",
                arg=menu,
                timeout=_remaining_seconds(deadline, "find_primary_preview") * 1_000,
            )
        control = expansion.as_element()
        if control is None:
            raise _layout_failure("actionable_primary_preview_control")
        phase = "expand_primary_preview"
        with _action_phase(phase):
            control.evaluate(
                "node => node.scrollIntoView({block:'center',inline:'nearest'})"
            )
            control.click(
                timeout=_remaining_seconds(deadline, "expand_primary_preview") * 1_000
            )
        phase = "expanded_primary_preview_render"
        with _action_phase(phase):
            completed = page.wait_for_function(
                "arg => {if (!arg.menu.isConnected) return {missing: true};"
                f"const record = ({script})(true).records"
                ".find(record => record.menu === arg.menu);"
                "if (!record) return false;"
                "return !record.payload.partial && record.payload.text !== arg.text"
                " ? record.payload : false;}",
                arg={"menu": menu, "text": payload["text"]},
                timeout=_remaining_seconds(deadline, "expanded_primary_preview_render")
                * 1_000,
            )
        expanded = completed.json_value()
        if isinstance(expanded, dict) and expanded.get("missing"):
            _inspect_failed_render(
                page,
                permalink=permalink,
                phase="expanded_primary_preview_render",
                expected_url=original_url,
            )
        if not isinstance(expanded, dict) or not expanded.get("missing"):
            issues = post_view_issues(expanded)
            if issues:
                raise _layout_failure(*issues, phase="post_view_validation")
        comparisons = {
            "expanded_primary_menu_preserved": not expanded.get("missing", False),
            "expanded_primary_url_unchanged": page.url == original_url,
            "expanded_primary_author_matches_source": (
                expanded.get("author") == payload["author"]
            ),
            "expanded_primary_publication_matches_source": (
                not expanded.get("missing", False)
                and _publication_matches(expanded, payload)
            ),
            "expanded_primary_body_complete": (
                not expanded.get("missing", False)
                and not expanded.get("partial", False)
            ),
            "expanded_primary_body_preserves_preview": preview_matches_expanded(
                payload, expanded.get("text", "")
            ),
            "expanded_primary_body_grew": (
                len(expanded.get("text", "")) > len(payload["text"])
            ),
        }
        missing = [name for name, matches in comparisons.items() if not matches]
        if missing:
            LOGGER.warning(
                "Mobile preview comparison failed",
                **comparisons,
                source_body_chars=len(payload["text"]),
                expanded_body_chars=len(expanded.get("text", "")),
                source_preview_prefix_chars=len(
                    payload.get("previewPrefix", payload["text"])
                ),
                source_preview_suffix_chars=len(payload.get("previewSuffix", "")),
            )
            raise _layout_failure(*missing, phase="expanded_primary_preview_render")
        LOGGER.debug(
            "Mobile post preview expanded",
            source_body_chars=len(payload["text"]),
            expanded_body_chars=len(expanded["text"]),
            menu_action_refreshed=(
                expanded.get("actionToken") != payload.get("actionToken")
            ),
        )
        return expanded
    except (PlaywrightError, TransientNavigationError) as exc:
        if getattr(exc, "_fbn_render_classified", False):
            raise
        phase = getattr(exc, "_fbn_failure_phase", phase)
        result = _inspect_failed_render(
            page, permalink=permalink, phase=phase, expected_url=original_url
        )
        if isinstance(exc, (PlaywrightTimeoutError, TransientNavigationError)):
            # Both timeout mechanisms still inspect the current screen.
            # Known readable UI may retry; missing/unsupported visible structure
            # must not bypass the same layout and security checks as a waiter.
            if result.get("missing") or not result.get("postCount"):
                raise _layout_failure(
                    *_missing_mobile_components(result), phase=phase
                ) from exc
            _raise_render_timeout(phase, loading_shell=False)
        raise _layout_failure("complete_expanded_primary_body", phase=phase) from exc
    finally:
        for handle in (completed, expansion):
            if handle is not None:
                with suppress(PlaywrightError):
                    handle.dispose()


def copy_post_link(page: Page, menu: ElementHandle, *, timeout_seconds: float) -> str:
    """Capture one fresh clipboard write while the identified card owns the menu."""

    deadline = time.monotonic() + timeout_seconds
    page.bring_to_front()
    _dismiss_app_prompt(page, deadline)
    copy = page.get_by_role("button", name="Copy link", exact=True)
    if any(copy.nth(index).is_visible() for index in range(copy.count())):
        raise _layout_failure("closed_previous_post_menu")
    opened = False
    page.evaluate(_CAPTURE_INSTALL_SCRIPT)
    try:
        with _action_phase("open_post_menu"):
            menu.click(timeout=_remaining_seconds(deadline, "open_post_menu") * 1_000)
        opened = True
        with _action_phase("wait_copy_link"):
            copy.wait_for(
                state="visible",
                timeout=_remaining_seconds(deadline, "wait_copy_link") * 1_000,
            )
        with _action_phase("copy_post_link"):
            _dismiss_app_prompt(page, deadline)
            copy.click(timeout=_remaining_seconds(deadline, "copy_post_link") * 1_000)
            page.wait_for_function(
                "window.__fbnCopyCapture.values.length > 0",
                timeout=_remaining_seconds(deadline, "fresh_clipboard_write") * 1_000,
            )
        values = page.evaluate("window.__fbnCopyCapture.values")
        if len(values) != 1 or not isinstance(values[0], str):
            raise _layout_failure("single_fresh_clipboard_link")
        return values[0]
    finally:
        # Never reuse a previous menu or clipboard value for the next card.
        cleanup_token = _SCAN_DEADLINE.set(None)
        try:
            if opened:
                close = page.get_by_role("button", name="Close", exact=True)
                # Cleanup gets its own small budget even after the action timed
                # out. Never leave clipboard interception installed on failure.
                cleanup_deadline = time.monotonic() + min(5, timeout_seconds)
                with _action_phase("close_post_menu"):
                    close.click(
                        timeout=_remaining_seconds(cleanup_deadline, "close_post_menu")
                        * 1_000
                    )
                    copy.wait_for(
                        state="hidden",
                        timeout=_remaining_seconds(cleanup_deadline, "closed_post_menu")
                        * 1_000,
                    )
        finally:
            try:
                page.evaluate(_CAPTURE_RESTORE_SCRIPT)
            finally:
                _SCAN_DEADLINE.reset(cleanup_token)


def _publication_label(payload: dict[str, Any]) -> str:
    # Permalink headers put the verified author and publication time together;
    # feed headers keep them separate. Strip only that same verified author.
    label = payload["timestampLabel"].strip(" ,")
    author = payload["author"]
    if label.startswith(author + ","):
        label = label[len(author) + 1 :].strip()
    return re.sub(r",\s*(?:Private|Public) group$", "", label).strip(" ,")


def _post_signature(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (payload["author"], payload["text"], _publication_label(payload))


def _relative_publication_window(
    payload: dict[str, Any],
) -> tuple[float, float] | None:
    """A rounded relative age denotes a range, not an exact publication time."""

    observed = payload.get("timestampObservedAt")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
    ):
        return None
    label = _publication_label(payload).lower()
    if label == "just now":
        count, unit = 0, "m"
    else:
        match = re.fullmatch(
            r"([0-9]{1,9})\s*([smhdw])|"
            r"([0-9]{1,9})\s+(seconds?|minutes?|hours?|days?|weeks?)\s+ago",
            label,
        )
        if match is None:
            return None
        count = int(match[1] or match[3])
        unit = (match[2] or match[4])[0]
    resolution = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    newest = observed - count * resolution * 1000
    return newest - resolution * 1000, newest


def _publication_matches(item: dict[str, Any], source: dict[str, Any]) -> bool:
    if _publication_label(item) == _publication_label(source):
        return True
    target_window = _relative_publication_window(item)
    source_window = _relative_publication_window(source)
    if target_window is None or source_window is None:
        return False
    # Only reconcile observations from this bounded scan. Missing timing proof,
    # backwards clocks, stale snapshots and non-overlapping ages stay rejected.
    elapsed = item["timestampObservedAt"] - source["timestampObservedAt"]
    budget_ms = (MAX_SCAN_SECONDS + COPY_TIMEOUT_SECONDS + MAX_LINK_SECONDS) * 1000
    if not 0 <= elapsed <= budget_ms:
        return False
    return max(source_window[0], target_window[0]) < min(
        source_window[1], target_window[1]
    )


def _primary_post_comparisons(
    item: dict[str, Any], payload: dict[str, Any]
) -> dict[str, bool]:
    return {
        "resolved_primary_author_matches_source": item["author"] == payload["author"],
        "resolved_primary_body_matches_source": item["text"] == payload["text"],
        "resolved_primary_body_complete": not (
            item.get("partial", False) or payload.get("partial", False)
        ),
        "resolved_primary_publication_matches_source": (
            _publication_matches(item, payload)
        ),
        "resolved_primary_body_or_media": bool(item["text"]) or item["mediaCount"] > 0,
        "resolved_primary_media_matches_source": bool(item["text"])
        or (
            bool(payload.get("mediaKeys"))
            and item.get("mediaKeys") == payload.get("mediaKeys")
        ),
    }


def _verify_primary_post(
    records: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    missing_components: tuple[str, ...] = (),
) -> None:
    """Require one matching post, reporting only safe comparison results."""

    if len(records) != 1:
        LOGGER.warning("Post link verification failed", primary_post_count=len(records))
        raise _layout_failure(
            "unique_resolved_primary_post",
            *missing_components,
            phase="post_verification",
        )
    item = records[0]
    comparisons = _primary_post_comparisons(item, payload)
    missing = [name for name, matches in comparisons.items() if not matches]
    if missing:
        LOGGER.warning(
            "Post link verification failed",
            author_matches=comparisons["resolved_primary_author_matches_source"],
            body_matches=comparisons["resolved_primary_body_matches_source"],
            media_matches=comparisons["resolved_primary_media_matches_source"],
            publication_matches=comparisons[
                "resolved_primary_publication_matches_source"
            ],
            publication_labels_equal=(
                _publication_label(item) == _publication_label(payload)
            ),
            source_relative_publication=(
                _relative_publication_window(payload) is not None
            ),
            target_relative_publication=(
                _relative_publication_window(item) is not None
            ),
            source_body_chars=len(payload["text"]),
            target_body_chars=len(item["text"]),
            source_partial=payload.get("partial", False),
            target_partial=item.get("partial", False),
            source_media_count=payload["mediaCount"],
            target_media_count=item["mediaCount"],
            source_text_parts=payload.get("bodyTextLengths", []),
            target_text_parts=item.get("bodyTextLengths", []),
            source_text_part_count=payload.get("bodyTextPartCount"),
            target_text_part_count=item.get("bodyTextPartCount"),
            source_body_controls=payload.get("bodyControlCount"),
            target_body_controls=item.get("bodyControlCount"),
        )
        raise _layout_failure(*missing, phase="post_verification")


def _verify_post_at_deadline(
    page: Page, records: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    """Explain the final observed failure without saving content or retrying."""

    result = _inspect_failed_render(
        page,
        permalink=True,
        phase="post_verification",
        verification_post_count=len(records),
    )
    _verify_primary_post(
        records, payload, missing_components=_missing_mobile_components(result)
    )


def resolve_post_link(
    target: Page,
    copied: str,
    payload: dict[str, Any],
    allowed_group_keys: frozenset[str],
    *,
    timeout_seconds: float,
) -> str:
    """Resolve a Facebook-owned share link and match only primary target content."""

    destinations: list[PostLink] = []

    def record_navigation(frame):
        if frame == target.main_frame:
            link = parse_post_url(frame.url)
            if (
                link is not None
                and len(destinations) <= 10
                and (not destinations or destinations[-1] != link)
            ):
                destinations.append(link)

    target.on("framenavigated", record_navigation)
    try:
        return _resolve_post_link(
            target,
            copied,
            payload,
            allowed_group_keys,
            destinations,
            timeout_seconds=timeout_seconds,
        )
    finally:
        target.remove_listener("framenavigated", record_navigation)


def _resolve_post_link(
    target: Page,
    copied: str,
    payload: dict[str, Any],
    allowed_group_keys: frozenset[str],
    destinations: list[PostLink],
    *,
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    if not _trusted_url(copied):
        raise _layout_failure("trusted_copied_post_link")
    direct = parse_post_url(copied)
    parsed = urlsplit(copied)
    if direct is None and (
        re.fullmatch(r"/share/p/[A-Za-z0-9]{1,200}/?", parsed.path) is None
        or parsed.query
        or parsed.fragment
    ):
        raise _layout_failure("trusted_copied_post_link")
    if direct is not None and direct.group_key not in allowed_group_keys:
        raise _layout_failure("copied_link_for_verified_group")
    target.bring_to_front()
    with _action_phase("post_navigation"):
        response = target.goto(
            copied,
            wait_until="domcontentloaded",
            timeout=_remaining_seconds(deadline, "post_navigation") * 1_000,
        )
    wait_for_mobile_page(
        target,
        timeout_seconds=_remaining_seconds(deadline, "post_render"),
        status=response.status if response is not None else None,
        permalink=True,
    )
    resolved = parse_post_url(target.url)
    # The post can render before its short URL is replaced by the client router.
    # Wait only on that same trusted short-link path, within the existing budget;
    # an unrelated/off-site/direct destination is still rejected immediately.
    while (
        resolved is None
        and direct is None
        and _trusted_url(target.url)
        and urlsplit(target.url).path == parsed.path
        and time.monotonic() < deadline
    ):
        target.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1_000)))
        resolved = parse_post_url(target.url)
    if resolved is None or resolved.group_key not in allowed_group_keys:
        raise _layout_failure("resolved_link_for_verified_group")
    if direct is not None and direct.post_id != resolved.post_id:
        raise _layout_failure("unchanged_direct_post_identity")
    expanded_preview = False
    while True:
        _check_scan_budget()
        # Capture router changes from navigation onward, including those that
        # happen before the first readable post. Do not adopt a replacement ID.
        if len(destinations) > 10 or any(
            link.post_id != resolved.post_id or link.group_key not in allowed_group_keys
            for link in destinations
        ):
            raise _layout_failure("unchanged_resolved_post_identity")
        if parse_post_url(target.url) != resolved:
            raise _layout_failure("unchanged_resolved_post_identity")
        with mobile_records(target, permalink=True) as records:
            items = [item for _, item in records]
            if (
                not expanded_preview
                and len(records) == 1
                and items[0].get("partial")
                and not payload.get("partial")
                and items[0]["author"] == payload["author"]
                and _publication_matches(items[0], payload)
                and items[0]["text"]
                and preview_matches_expanded(items[0], payload["text"])
            ):
                # The permalink can be shortened too. Expand only this
                # matching preview, inside the original link deadline, and
                # still require full equality and unchanged canonical identity.
                items = [
                    expand_post_preview(
                        target,
                        records[0][0],
                        items[0],
                        timeout_seconds=min(
                            MAX_EXPANSION_SECONDS,
                            _remaining_seconds(deadline, "expand_resolved_preview"),
                        ),
                        permalink=True,
                    )
                ]
                expanded_preview = True
                if (
                    parse_post_url(target.url) != resolved
                    or len(destinations) > 10
                    or any(
                        link.post_id != resolved.post_id
                        or link.group_key not in allowed_group_keys
                        for link in destinations
                    )
                ):
                    raise _layout_failure("unchanged_resolved_post_identity")
        # A supported header/footer can arrive before the body finishes
        # rendering. Re-read within the original deadline without relaxing
        # matching or logging every poll.
        if len(items) == 1 and all(
            _primary_post_comparisons(items[0], payload).values()
        ):
            break
        if time.monotonic() >= deadline:
            _verify_post_at_deadline(target, items, payload)
        target.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1_000)))
    return resolved.url


@contextmanager
def _post_target(
    context: BrowserContext,
    *,
    on_error: Callable[[Page, Exception], None] | None = None,
) -> Iterator[Page]:
    target = context.new_page()

    def guard(route: Route) -> None:
        request = route.request
        if request.is_navigation_request() and not _trusted_url(request.url):
            route.abort()
        else:
            route.fallback()

    try:
        target.route("**/*", guard)
        yield target
    except Exception as exc:
        if on_error is not None and not isinstance(exc, _ScanBudgetExpired):
            with suppress(Exception):
                on_error(target, exc)
        raise
    finally:
        with suppress(PlaywrightError):
            target.close()


class MobilePostSource(BrowserSession):
    """A separately implemented mobile reader sharing only browser ownership."""

    def _launch_context(
        self,
        playwright: Any,
        *,
        profile_dir: Path,
        headless: bool,
        timezone_id: str | None,
    ) -> BrowserContext:
        options = dict(playwright.devices["Pixel 7"])
        options.pop("default_browser_type", None)
        options.update(headless=headless, accept_downloads=False)
        if timezone_id is not None:
            options["timezone_id"] = timezone_id
        if self.settings.browser == "executable":
            options["executable_path"] = str(self.settings.executable_path)
        else:
            options["channel"] = self.settings.browser
        try:
            return playwright.chromium.launch_persistent_context(
                str(profile_dir), **options
            )
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                "The mobile browser could not be started. Run `fbn doctor`."
            ) from exc

    def _validate_authenticated_profile(
        self,
        group: GroupRef,
        *,
        navigation_timeout_seconds: float,
        acquire_lock: bool = True,
    ) -> PageState:
        LOGGER.debug(
            "Authentication profile validation started",
            group_key=group.key,
            timeout_seconds=navigation_timeout_seconds,
        )
        with self._context(
            headless=True,
            acquire_lock=acquire_lock,
        ) as context:
            page: Page | None = None
            try:
                page = context.new_page()
                page.set_default_timeout(navigation_timeout_seconds * 1_000)
                page.set_default_navigation_timeout(navigation_timeout_seconds * 1_000)
                with initial_group_navigation(page) as navigation:
                    response = page.goto(
                        mobile_group_url(group), wait_until="domcontentloaded"
                    )
                    state = wait_for_mobile_page(
                        page,
                        status=response.status if response else None,
                        timeout_seconds=navigation_timeout_seconds,
                    )
                    verified_group_keys(
                        response, group, page.url, navigation=navigation
                    )
                return state
            except PlaywrightTimeoutError as exc:
                capture_failure_screenshots(
                    self.settings.profile_dir, exc, {"feed": page}
                )
                raise TransientNavigationError(
                    "Facebook group validation timed out during authentication "
                    "bootstrap."
                ) from exc
            except PlaywrightError as exc:
                capture_failure_screenshots(
                    self.settings.profile_dir, exc, {"feed": page}
                )
                raise TransientNavigationError(
                    "Facebook group navigation failed during authentication bootstrap."
                ) from exc
            except Exception as exc:
                capture_failure_screenshots(
                    self.settings.profile_dir, exc, {"feed": page}
                )
                raise
            finally:
                if page is not None:
                    with suppress(PlaywrightError):
                        page.close()

    def fetch_recent(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        boundary_post_id: str | None = None,
    ) -> ScanResult:
        with self._context(
            headless=self.settings.headless, timezone_id=policy.timezone_name
        ) as context:
            page: Page | None = None

            # Reject direct off-site navigations. Final redirect destinations
            # are independently checked before any post is accepted.
            def guard(route):
                request = route.request
                if request.is_navigation_request() and not _trusted_url(request.url):
                    route.abort()
                else:
                    route.fallback()

            try:
                page = context.new_page()
                page.route("**/*", guard)
                with initial_group_navigation(page) as navigation:
                    response = page.goto(
                        mobile_group_url(group),
                        wait_until="domcontentloaded",
                        timeout=policy.navigation_timeout_seconds * 1_000,
                    )
                    wait_for_mobile_page(
                        page,
                        timeout_seconds=policy.navigation_timeout_seconds,
                        status=response.status if response else None,
                    )
                    aliases = verified_group_keys(
                        response, group, page.url, navigation=navigation
                    )
                select_new_posts(
                    page, timeout_seconds=policy.navigation_timeout_seconds
                )
                wait_for_mobile_page(
                    page, timeout_seconds=policy.navigation_timeout_seconds
                )
                if _group_key(page.url) not in aliases:
                    raise AccessDeniedError("Sorting left the verified mobile group.")
                return self._scan_mobile(
                    page, context, group, policy, aliases, boundary_post_id
                )
            except PlaywrightError as exc:
                capture_failure_screenshots(
                    self.settings.profile_dir, exc, {"feed": page}
                )
                raise TransientNavigationError(
                    "Mobile acquisition failed; no immediate retry was attempted."
                ) from exc
            except Exception as exc:
                capture_failure_screenshots(
                    self.settings.profile_dir, exc, {"feed": page}
                )
                raise
            finally:
                if page is not None:
                    with suppress(PlaywrightError):
                        page.close()

    def _scan_mobile(
        self,
        page: Page,
        context: BrowserContext,
        group: GroupRef,
        policy: ScanPolicy,
        aliases: frozenset[str],
        boundary_post_id: str | None,
    ) -> ScanResult:
        accumulated: list[Post] = []
        progress = [0]
        token = _SCAN_DEADLINE.set(time.monotonic() + MAX_SCAN_SECONDS)
        try:
            return self._scan_mobile_views(
                page,
                context,
                group,
                policy,
                aliases,
                boundary_post_id,
                accumulated,
                progress,
            )
        except _ScanBudgetExpired:
            LOGGER.info(
                "Mobile scan budget reached; retaining verified posts only",
                post_count=len(accumulated),
                scroll_count=progress[0],
            )
            return self._scan_result(
                accumulated,
                progress[0],
                True,
                boundary_post_id,
                stop_reason="scan_budget",
            )
        finally:
            _SCAN_DEADLINE.reset(token)

    def _scan_mobile_views(
        self,
        page: Page,
        context: BrowserContext,
        group: GroupRef,
        policy: ScanPolicy,
        aliases: frozenset[str],
        boundary_post_id: str | None,
        accumulated: list[Post],
        progress: list[int],
    ) -> ScanResult:
        started = time.monotonic()
        observed_at = datetime.now(timezone.utc)
        seen: set[tuple] = set()
        seen_ids: set[str] = set()
        verified_links: dict[str, tuple[dict[str, Any], str]] = {}
        marker_pass = None
        stagnant = 0
        scrolls = 0
        post_limit = (
            policy.sample_count * (policy.max_scrolls + 1)
            if boundary_post_id
            else policy.sample_count
        )
        for scan_index in range(policy.max_scrolls + 1):
            page.bring_to_front()
            wait_for_mobile_page(
                page, timeout_seconds=policy.navigation_timeout_seconds
            )
            if _group_key(page.url) not in aliases:
                raise AccessDeniedError("The mobile scan left the verified group.")
            before = len(accumulated)
            with mobile_records(page) as records:
                scroll_signature = [(p.get("cardToken"), p["text"]) for _, p in records]
                for menu, payload in records:
                    signature = (
                        payload["cardToken"],
                        payload["actionToken"],
                        *_post_signature(payload),
                    )
                    if signature in seen:
                        continue
                    if (
                        len(accumulated) >= post_limit
                        or time.monotonic() - started >= MAX_SCAN_SECONDS
                    ):
                        return self._scan_result(
                            accumulated,
                            scrolls,
                            True,
                            boundary_post_id,
                            stop_reason="post_limit"
                            if len(accumulated) >= post_limit
                            else "scan_budget",
                        )
                    preview_signature = signature
                    # Bind the link to the original menu before expansion can
                    # refresh Bloks action IDs or recycle a menu action. Full
                    # destination equality still verifies this original link.
                    copied = copy_post_link(
                        page,
                        menu,
                        timeout_seconds=min(
                            COPY_TIMEOUT_SECONDS, policy.navigation_timeout_seconds
                        ),
                    )
                    if payload.get("partial"):
                        payload = expand_post_preview(
                            page,
                            menu,
                            payload,
                            timeout_seconds=min(
                                MAX_EXPANSION_SECONDS,
                                policy.navigation_timeout_seconds,
                                _remaining_seconds(
                                    started + MAX_SCAN_SECONDS, "expand_primary_preview"
                                ),
                            ),
                        )
                        signature = (
                            payload["cardToken"],
                            payload["actionToken"],
                            *_post_signature(payload),
                        )
                    cached = verified_links.get(copied)
                    if cached is not None and all(
                        _primary_post_comparisons(payload, cached[0]).values()
                    ):
                        # Re-rendered cards have new action/DOM tokens. Reuse a
                        # result only after freshly copying the SAME link and
                        # matching the complete source again. Never deduplicate
                        # by text/author or a temporary action ID alone.
                        href = cached[1]
                        LOGGER.debug("Verified copied link reused within scan")
                    else:
                        # Close destinations promptly: at most feed + one post.
                        with _post_target(
                            context,
                            on_error=lambda target, error: capture_failure_screenshots(
                                self.settings.profile_dir,
                                error,
                                {"post": target, "feed": page},
                            ),
                        ) as target:
                            href = resolve_post_link(
                                target,
                                copied,
                                payload,
                                aliases,
                                timeout_seconds=min(
                                    MAX_LINK_SECONDS, policy.navigation_timeout_seconds
                                ),
                            )
                        verified_links[copied] = (payload.copy(), href)
                    posts = extract_posts(
                        [{**payload, "href": href}],
                        group,
                        observed_at,
                        1,
                        allowed_group_keys=aliases,
                        timezone_name=policy.timezone_name,
                    )
                    if len(posts) != 1:
                        raise _layout_failure("valid_canonical_mobile_post")
                    post = replace(
                        posts[0],
                        published_at=parse_facebook_timestamp(
                            _publication_label(payload),
                            observed_at,
                            timezone_name=policy.timezone_name,
                        )
                        or posts[0].published_at,
                    )
                    seen.add(signature)
                    seen.add(preview_signature)
                    if post.post_id in seen_ids:
                        continue
                    seen_ids.add(post.post_id)
                    accumulated.append(replace(post, position=len(accumulated)))
                    if post.post_id == boundary_post_id:
                        marker_pass = scan_index
            stagnant = stagnant + 1 if len(accumulated) == before else 0
            # Read at least one further pass after the marker, for overlap.
            if marker_pass is not None and scan_index > marker_pass:
                return self._scan_result(
                    accumulated,
                    scrolls,
                    False,
                    boundary_post_id,
                    stop_reason="marker_overlap_complete",
                )
            if stagnant >= policy.stagnant_scrolls or scan_index == policy.max_scrolls:
                stop_reason = (
                    "stagnant_feed"
                    if stagnant >= policy.stagnant_scrolls
                    else "scroll_limit"
                )
                break
            page.bring_to_front()
            page.evaluate(
                "() => window.scrollBy(0, Math.max(window.innerHeight * 0.8, 600))"
            )
            scrolls += 1
            progress[0] = scrolls
            self._wait_for_scroll_progress(page, policy, scroll_signature)
        return self._scan_result(
            accumulated, scrolls, True, boundary_post_id, stop_reason=stop_reason
        )

    @staticmethod
    def _wait_for_scroll_progress(
        page: Page, policy: ScanPolicy, previous: list[tuple[Any, str]]
    ) -> None:
        if policy.settle_seconds <= 0:
            return
        # An idle timeout is not a completed layout. The next pass still checks
        # every visible primary card under the remaining navigation/scan budget.
        try:
            page.wait_for_function(
                f"previous => JSON.stringify(({MOBILE_SCAN_SCRIPT})(true).records"
                ".map(r => [r.payload.cardToken, r.payload.text])) !== "
                "JSON.stringify(previous)",
                arg=previous,
                timeout=_remaining_seconds(
                    time.monotonic() + policy.settle_seconds, "scroll_progress"
                )
                * 1_000,
            )
        except PlaywrightTimeoutError:
            _check_scan_budget()

    @staticmethod
    def _scan_result(
        posts: list[Post],
        scrolls: int,
        bounded: bool,
        boundary_post_id: str | None,
        *,
        stop_reason: str = "unspecified",
    ) -> ScanResult:
        # Mobile can promote a story in the visible list. Order by the full
        # accessible publication time when every card provides a parsed time.
        # Unknown dates must not become an excuse to close a catch-up gap.
        if posts and all(post.published_at is not None for post in posts):
            # Relative labels are coarse (several posts may say "2h"). Include
            # unnotified ties before the marker rather than silently skipping
            # a newer post that Facebook placed just below it.
            posts = sorted(
                posts,
                key=lambda post: (
                    post.published_at,
                    post.post_id != boundary_post_id,
                ),
                reverse=True,
            )
        elif boundary_post_id:
            bounded = True
            stop_reason = "unknown_publication_time"
            LOGGER.warning(
                "Mobile post order uncertain",
                missing_components="publication_time",
                post_count=len(posts),
            )
        posts = [replace(post, position=index) for index, post in enumerate(posts)]
        marker_found = any(post.post_id == boundary_post_id for post in posts)
        LOGGER.info(
            "Mobile scan coverage",
            stop_reason=stop_reason,
            marker_found=marker_found,
            post_count=len(posts),
            scroll_count=scrolls,
            bounded=bounded,
        )
        if boundary_post_id and (not marker_found or bounded):
            LOGGER.warning(
                "Mobile catch-up coverage incomplete",
                marker_found=marker_found,
                post_count=len(posts),
                scroll_count=scrolls,
                bounded=bounded,
            )
        return ScanResult(tuple(posts), PageState.FEED.value, scrolls, bounded)
