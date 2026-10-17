"""Mobile Facebook acquisition using verified primary-post Copy link actions."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
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
from .exceptions import (
    AccessDeniedError,
    BrowserUnavailableError,
    ConfigurationError,
    LayoutChangedError,
    TransientNavigationError,
)
from .extractor import (
    _FACEBOOK_TIMESTAMP_RE,
    FACEBOOK_HOSTS,
    PostLink,
    extract_posts,
    parse_facebook_timestamp,
    parse_group_ref,
    parse_post_url,
)
from .failure_screenshots import capture_failure_screenshots
from .logging import get_logger
from .models import GroupRef, Post, ScanPolicy, ScanResult

LOGGER = get_logger("mobile")
MAX_SCAN_SECONDS = 90.0
COPY_TIMEOUT_SECONDS = 30.0
MAX_LINK_SECONDS = 30.0
MAX_EXPANSION_SECONDS = 15.0

# Bloks cards are direct children of a vscroller. The contextual menu belongs
# to the primary header; its author must agree with that header's visible link.
# Neither the footer nor any following comment preview is a content section.
MOBILE_SCAN_SCRIPT = r"""
(includeContent) => {
  const permalink = false;
  const clean = value => (value || '').normalize('NFKC')
    .replace(/[\u034f\u200b\u200e\u200f\ufeff]/g, '')
    .replace(/\s+/g, ' ').trim();
  // Bloks leaves off-screen cards in the DOM with content-visibility:auto.
  // Their boxes exist, but innerText is empty until scrolling renders them.
  const visible = node => node.checkVisibility({
    contentVisibilityAuto: true, visibilityProperty: true, opacityProperty: true,
  });
  const menuSelector = '[role="button"][aria-label^="More options for "]';
  const textSelector = '[data-mcomponent="ServerTextArea"],'
    + '[data-mcomponent="TextArea"]';
  const timestampPattern = new RegExp(__FBN_TIMESTAMP_PATTERN__, 'i');
  const actionLabel = node => clean(node.getAttribute('aria-label'))
    .replace(/\p{Co}/gu, '').trim().replace(/^\d+\s*/, '').toLowerCase();
  const excludedSelector = '[role="article"],[aria-label^="Comment by "],'
    + '[aria-label^="Reply by "],[aria-label="Shared post"],'
    + '[data-ad-rendering-role="comment"]';
  const roots = Array.from(document.querySelectorAll(
    '[data-mcomponent="MContainer"][data-type="vscroller"]'
  )).filter(root => visible(root) && !root.parentElement.closest(
    '[data-type="vscroller"]'
  ));
  const records = [];
  const observedAt = Date.now();
  const tokens = window.__fbnMobileCardTokens ||= {next: 0, cards: new WeakMap()};
  const missing = new Set();
  let candidateCount = 0;
  if (roots.length !== 1) missing.add('unique_mobile_scroller');
  const branch = (root, node) => {
    while (node && node.parentElement !== root) node = node.parentElement;
    return node;
  };
  const sectionsFor = card => Array.from(card.children).filter(node =>
    node.hasAttribute('data-mcomponent') || node.querySelector('[data-mcomponent]')
  );
  const primaryMenus = card => Array.from(card.querySelectorAll(menuSelector))
    .filter(menu => visible(menu) && !menu.closest(excludedSelector)
      && sectionsFor(card).slice(0, 2).includes(branch(card, menu)));
  const action = (section, pattern) => Array.from(section.querySelectorAll(
    '[role="button"]'
  )).some(node => !node.closest(excludedSelector) && node.checkVisibility({
    visibilityProperty: true, opacityProperty: true,
  })
    && pattern.test(actionLabel(node)));
  const footerIndex = (sections, start) => {
    for (let index = start; index < sections.length; index++) {
      const like = action(sections[index], /^(?:like|unlike)(?:,|$)/);
      const comment = action(sections[index], /^(?:make a comment|comments?)$/);
      // The first action row belongs to the post. If it is incomplete, wait
      // for it; never scan through replies to find a later substitute footer.
      // Its aria labels remain usable when content-visibility:auto defers paint.
      if (like || comment) return like && comment ? index : -1;
    }
    return -1;
  };
  for (const root of roots) {
    let cards = Array.from(root.children);
    if (permalink) {
      // Single-post pages can put header/body/actions directly in the scroller.
      // Only use that shape when one primary header precedes its own footer.
      const menus = primaryMenus(root), sections = sectionsFor(root);
      if (menus.length === 1) {
        const start = sections.indexOf(branch(root, menus[0])) + 1;
        if (footerIndex(sections, start) >= start) cards = [root];
      }
    }
    for (const card of cards) {
      const primary = primaryMenus(card);
      if (!primary.length) continue;
      candidateCount++;
      if (primary.length !== 1) { missing.add('unique_primary_menu'); continue; }
      const menu = primary[0];
      const header = branch(card, menu);
      const owner = clean(menu.getAttribute('aria-label').slice(17));
      const authors = Array.from(header.querySelectorAll('[role="link"],a'))
        .filter(node => visible(node) && clean(node.innerText) === owner);
      if (!owner || authors.length !== 1) {
        missing.add('matching_primary_author'); continue;
      }
      const timestampLabel = node => {
        const stripOwner = value => value.startsWith(owner + ',')
          ? value.slice(owner.length + 1).replace(/^[,\s]+/, '') : value;
        let label = stripOwner(clean(node.getAttribute('aria-label'))
          .replace(/^[,\s]+/, ''));
        // Metadata prefixes must also exist as rendered, non-link text within
        // this exact wrapper. Do not maintain a growing list of badge names or
        // strip an unverified author's name merely because it precedes a date.
        for (let pass = 0; pass < 3; pass++) {
          const date = label.replace(/, (?:Private|Public) group$/, '');
          if (timestampPattern.test(date)) break;
          const prefix = Array.from(node.querySelectorAll(textSelector))
            .filter(child => visible(child) && !child.closest('[role="link"],a')
              && !child.querySelector('[role="link"],a')
              && !child.querySelector(menuSelector))
            .map(child => clean(child.innerText))
            .find(value => value && value.length <= 80
              && label.startsWith(value + ','));
          if (!prefix) break;
          // Permalink metadata can place a badge before the verified author.
          label = stripOwner(label.slice(prefix.length + 1)
            .replace(/^[,\s]+/, ''));
        }
        return label;
      };
      const timestamps = Array.from(header.querySelectorAll(textSelector)).filter(
        node => visible(node) && clean(node.getAttribute('aria-label'))
          && !authors[0].contains(node)
      ).map(node => ({node, label: timestampLabel(node)})).filter(item =>
        timestampPattern.test(item.label.replace(/, (?:Private|Public) group$/, ''))
      );
      // Metadata wrappers may contain the verified author and contributor badge
      // as well as the date. Only a recognized date label can qualify, and no
      // wrapper containing a post menu can become a timestamp.
      for (const node of header.querySelectorAll(
        '[data-mcomponent="MContainer"][aria-label]'
      )) {
        if (!visible(node) || node.contains(menu) || node.querySelector(menuSelector)
          || authors[0].contains(node)) continue;
        const label = timestampLabel(node);
        const date = label.replace(/, (?:Private|Public) group$/, '');
        if (timestampPattern.test(date)) timestamps.push({node, label});
      }
      const distinctTimestamps = timestamps.filter(item => !timestamps.some(
        other => other !== item && item.node.contains(other.node)
          && item.label === other.label
      ));
      if (distinctTimestamps.length !== 1) {
        missing.add('unique_primary_timestamp'); continue;
      }
      const timestamp = distinctTimestamps[0];
      const sections = sectionsFor(card);
      const start = sections.indexOf(header) + 1;
      const end = footerIndex(sections, start);
      if (end < start) { missing.add('primary_action_footer'); continue; }
      const texts = [];
      const expanders = [];
      let mediaCount = 0;
      let partial = false;
      let bodyControlCount = 0;
      for (const section of sections.slice(start, end)) {
        const excluded = Array.from(section.querySelectorAll(excludedSelector));
        for (const nestedMenu of section.querySelectorAll(menuSelector)) {
          const nested = branch(section, nestedMenu);
          if (nested) excluded.push(nested);
        }
        const own = node => !node.closest(excludedSelector)
          && !excluded.some(nested => nested.contains(node));
        // Bloks can render truncation as a plain inline span inside an
        // actionable TextArea, with no button role. Never treat its UI label
        // as post content or mistake the preview for a complete body.
        const candidates = Array.from(section.querySelectorAll(
          'span,button,[role="button"]'
        )).filter(node => visible(node) && own(node)
          && (/^(?:\.{3}|…)\s*see more$/i.test(clean(node.innerText))
            || (/^see more$/i.test(clean(node.innerText))
              && node.closest('button,[role="button"]')))
          && !node.closest('a,[role="link"]')
          && section.contains(node.closest('button,[role="button"],'
            + '[data-mcomponent="TextArea"][data-action-id],'
            + '[data-mcomponent="ServerTextArea"][data-action-id]')));
        const controlsToExpand = candidates.filter(node => !candidates.some(
          other => other !== node && node.contains(other)
        ));
        expanders.push(...controlsToExpand);
        excluded.push(...controlsToExpand);
        const textNodes = [section, ...section.querySelectorAll(textSelector)]
          .filter(node => node.matches(textSelector) && visible(node) && own(node)
            && !node.parentElement.closest(textSelector));
        for (const node of textNodes) {
          let raw = node.innerText;
          if (excluded.some(nested => node.contains(nested))) {
            const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
            const parts = [];
            while (walker.nextNode()) {
              if (own(walker.currentNode.parentElement)) {
                parts.push(walker.currentNode.textContent);
              }
            }
            raw = parts.join(' ');
          }
          const text = clean(raw);
          if (text && !texts.includes(text)) texts.push(text);
        }
        mediaCount += Array.from(section.querySelectorAll(
          '[data-mcomponent="ServerImageArea"],[data-mcomponent="ImageArea"],img,video'
        )).filter(node => visible(node) && own(node)).length;
        const controls = Array.from(section.querySelectorAll('[role="button"]'))
          .filter(node => own(node));
        bodyControlCount += controls.filter(visible).length;
        partial ||= controlsToExpand.length > 0;
      }
      const text = clean(texts.join(' '));
      if (!text && !mediaCount) { missing.add('primary_body_or_media'); continue; }
      if (!tokens.cards.has(card)) tokens.cards.set(card, ++tokens.next);
      let expand = expanders.length === 1 ? expanders[0].closest(
        'button,[role="button"],[data-mcomponent="TextArea"][data-action-id],'
        + '[data-mcomponent="ServerTextArea"][data-action-id]'
      ) : null;
      // Bloks text can have pointer-events:none. Its enclosing body action
      // region owns the click and can have a different action ID.
      const actionRegion = expand?.closest(
        '[data-mcomponent="MContainer"][data-action-id]'
      );
      if (actionRegion && sections.slice(start, end).some(
        section => section.contains(actionRegion)
      )) expand = actionRegion;
      records.push({card, menu, expand,
        payload: {
        author: owner, text, timestamp: clean(timestamp.node.innerText),
        timestampLabel: timestamp.label,
        timestampObservedAt: observedAt,
        cardToken: tokens.cards.get(card),
        actionToken: menu.getAttribute('data-action-id'),
        mediaCount, partial, position: records.length,
        bodyControlCount, bodyTextPartCount: texts.length,
        bodyTextLengths: texts.slice(0, 32).map(text => text.length),
        previewControlCount: expanders.length,
      }});
    }
  }
  const counts = {
    feedRootCount: roots.length, itemCount: candidateCount,
    containerCount: candidateCount, authorCount: records.length,
    storyMessageCount: records.length, postCount: records.length,
    hasFeed: roots.length === 1, missing: Array.from(missing),
  };
  return includeContent ? {...counts, records} : counts;
}
""".replace("__FBN_TIMESTAMP_PATTERN__", json.dumps(_FACEBOOK_TIMESTAMP_RE.pattern))

MOBILE_PERMALINK_SCAN_SCRIPT = MOBILE_SCAN_SCRIPT.replace(
    "const permalink = false;", "const permalink = true;"
)

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


def _layout_failure(*missing: str) -> LayoutChangedError:
    # All component names are constants, never server content or selectors.
    components = ",".join(sorted(set(missing)))
    LOGGER.warning("Mobile layout incomplete", missing_components=components)
    return LayoutChangedError(f"Mobile layout incomplete: missing={components}.")


def _remaining_seconds(deadline: float, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        LOGGER.warning("Mobile action timed out", phase=phase)
        raise TransientNavigationError(
            f"Mobile action timed out: phase={phase}; no immediate retry was attempted."
        )
    return remaining


@contextmanager
def _action_phase(phase: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    except PlaywrightError:
        LOGGER.warning(
            "Mobile browser action failed",
            phase=phase,
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        raise


def _log_layout_snapshot(
    page: Page,
    result: dict[str, Any],
    *,
    permalink: bool,
    phase: str = "page_render",
    verification_post_count: int | None = None,
    signals: PageSignals | None = None,
) -> None:
    # Fixed structural counters only. Never log text, labels, URLs, or IDs.
    structure = page.evaluate(
        """() => ({
          document_ready: document.readyState,
          document_visible: document.visibilityState === 'visible',
          mobile_screens: document.querySelectorAll(
            '[data-mcomponent="MScreen"]'
          ).length,
          total_scrollers: document.querySelectorAll('[data-type="vscroller"]').length,
          primary_menus: document.querySelectorAll(
            '[role="button"][aria-label^="More options for "]'
          ).length,
          visible_primary_menus: Array.from(document.querySelectorAll(
            '[role="button"][aria-label^="More options for "]'
          )).filter(node => node.checkVisibility({contentVisibilityAuto:true,
            visibilityProperty:true,opacityProperty:true})).length,
          loading_indicators: document.querySelectorAll('[role="progressbar"]').length,
          dialogs: document.querySelectorAll('[role="dialog"]').length,
        })"""
    )
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
        **terminal_signals,
        **structure,
    )


def _missing_mobile_components(result: dict[str, Any]) -> tuple[str, ...]:
    missing = result.get("missing", [])
    if not missing and result.get("postCount", 0) == 0:
        missing = ["primary_mobile_card"]
    return tuple(sorted(set(missing)))


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
        signals = read_page_signals(page, status=status, dom_scan_script=script)
        try:
            state = classify_page(signals)
        except LayoutChangedError:
            if time.monotonic() >= deadline:
                result = page.evaluate(script, False)
                missing = result.get("missing", [])
                _log_layout_snapshot(page, result, permalink=permalink)
                raise _layout_failure(*(missing or ["primary_mobile_card"])) from None
            page.wait_for_timeout(100)
            continue
        result = page.evaluate(script, False)
        if result.get("missing"):
            if time.monotonic() >= deadline:
                _log_layout_snapshot(page, result, permalink=permalink)
                raise _layout_failure(*result["missing"])
            page.wait_for_timeout(100)
            continue
        return state


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
    handle = _control(page, "New posts", feed_only=True)
    if handle is not None:
        handle.dispose()
        return
    sort = _control(page, "Sort")
    if sort is None:
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
) -> Iterator[list[tuple[ElementHandle, dict[str, Any]]]]:
    script = MOBILE_PERMALINK_SCAN_SCRIPT if permalink else MOBILE_SCAN_SCRIPT
    root = page.evaluate_handle(script, True)
    handles = []
    try:
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
                records.append((menu, payload_handle.json_value()))
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
    try:
        page.bring_to_front()
        menu.evaluate("node => node.scrollIntoView({block:'center',inline:'nearest'})")
        with _action_phase("find_primary_preview"):
            expansion = page.wait_for_function(
                f"menu => ({script})(true).records"
                ".find(record => record.menu === menu)?.expand || null",
                arg=menu,
                timeout=_remaining_seconds(deadline, "find_primary_preview") * 1_000,
            )
        control = expansion.as_element()
        if control is None:
            raise _layout_failure("actionable_primary_preview_control")
        control.evaluate(
            "node => node.scrollIntoView({block:'center',inline:'nearest'})"
        )
        with _action_phase("expand_primary_preview"):
            control.click(
                timeout=_remaining_seconds(deadline, "expand_primary_preview") * 1_000
            )
        with _action_phase("expanded_primary_preview_render"):
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
        if (
            page.url != original_url
            or expanded.get("actionToken") != payload["actionToken"]
            or expanded.get("author") != payload["author"]
            or not _publication_matches(expanded, payload)
        ):
            raise _layout_failure("unchanged_expanded_primary_post")
        if (
            expanded.get("partial")
            or not payload["text"]
            or not expanded["text"].startswith(payload["text"])
            or len(expanded["text"]) <= len(payload["text"])
        ):
            raise _layout_failure("complete_expanded_primary_body")
        LOGGER.debug(
            "Mobile post preview expanded",
            source_body_chars=len(payload["text"]),
            expanded_body_chars=len(expanded["text"]),
        )
        return expanded
    except PlaywrightError as exc:
        raise _layout_failure("complete_expanded_primary_body") from exc
    finally:
        for handle in (completed, expansion):
            if handle is not None:
                with suppress(PlaywrightError):
                    handle.dispose()


def copy_post_link(page: Page, menu: ElementHandle, *, timeout_seconds: float) -> str:
    """Capture one fresh clipboard write while the identified card owns the menu."""

    deadline = time.monotonic() + timeout_seconds
    page.bring_to_front()
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
            page.evaluate(_CAPTURE_RESTORE_SCRIPT)


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
        raise _layout_failure("unique_resolved_primary_post", *missing_components)
    item = records[0]
    comparisons = _primary_post_comparisons(item, payload)
    missing = [name for name, matches in comparisons.items() if not matches]
    if missing:
        LOGGER.warning(
            "Post link verification failed",
            author_matches=comparisons["resolved_primary_author_matches_source"],
            body_matches=comparisons["resolved_primary_body_matches_source"],
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
        raise _layout_failure(*missing)


def _verify_post_at_deadline(
    page: Page, records: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    """Explain the final observed failure without saving content or retrying."""

    signals = read_page_signals(page, dom_scan_script=MOBILE_PERMALINK_SCAN_SCRIPT)
    result = page.evaluate(MOBILE_PERMALINK_SCAN_SCRIPT, False)
    _log_layout_snapshot(
        page,
        result,
        permalink=True,
        phase="post_verification",
        verification_post_count=len(records),
        signals=signals,
    )
    # Account/access/loading state can change after the initial readable render.
    # Preserve typed terminal errors; unknown layouts still fail closed below.
    with suppress(LayoutChangedError):
        classify_page(signals)
    _verify_primary_post(
        records, payload, missing_components=_missing_mobile_components(result)
    )


def _has_additional_body_panel(item: dict[str, Any], source: dict[str, Any]) -> bool:
    """A panel can trigger one re-render, never qualify a mismatched post."""

    comparisons = _primary_post_comparisons(item, source)
    return (
        comparisons["resolved_primary_author_matches_source"]
        and comparisons["resolved_primary_publication_matches_source"]
        and bool(source["text"])
        and item["text"] != source["text"]
        and item["text"].startswith(source["text"] + " ")
        and not source.get("partial", False)
        and not item.get("partial", False)
        and source.get("bodyControlCount") == 0
        and item.get("bodyControlCount", 0) >= 2
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
    checked_canonical = False
    expanded_preview = False
    while True:
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
                and payload["text"].startswith(items[0]["text"])
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
        if (
            not checked_canonical
            and len(items) == 1
            and _has_additional_body_panel(items[0], payload)
        ):
            # Share pages sometimes append an unrelated interface panel to the
            # body. Re-render the already resolved mobile post once, then still
            # require full equality. Never follow a different identity, extend
            # the budget, or retry an account/access/navigation failure.
            wait_for_mobile_page(
                target,
                timeout_seconds=_remaining_seconds(deadline, "canonical_post_check"),
                permalink=True,
            )
            checked_canonical = True
            LOGGER.info("Checking canonical post after additional body controls")
            with _action_phase("canonical_post_navigation"):
                response = target.goto(
                    "https://m.facebook.com" + urlsplit(resolved.url).path,
                    wait_until="domcontentloaded",
                    timeout=_remaining_seconds(deadline, "canonical_post_navigation")
                    * 1_000,
                )
            wait_for_mobile_page(
                target,
                timeout_seconds=_remaining_seconds(deadline, "canonical_post_render"),
                status=response.status if response is not None else None,
                permalink=True,
            )
            continue
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
        if on_error is not None:
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
        started = time.monotonic()
        observed_at = datetime.now(timezone.utc)
        accumulated: list[Post] = []
        seen: set[tuple] = set()
        seen_ids: set[str] = set()
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
                            accumulated, scrolls, True, boundary_post_id
                        )
                    preview_signature = signature
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
                    copied = copy_post_link(
                        page,
                        menu,
                        timeout_seconds=min(
                            COPY_TIMEOUT_SECONDS, policy.navigation_timeout_seconds
                        ),
                    )
                    # Close each destination promptly so navigation history,
                    # decoded media and document heaps cannot grow across posts.
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
                return self._scan_result(accumulated, scrolls, False, boundary_post_id)
            if stagnant >= policy.stagnant_scrolls or scan_index == policy.max_scrolls:
                break
            page.bring_to_front()
            page.evaluate(
                "() => window.scrollBy(0, Math.max(window.innerHeight * 0.8, 600))"
            )
            scrolls += 1
            page.wait_for_timeout(policy.settle_seconds * 1_000)
        return self._scan_result(accumulated, scrolls, True, boundary_post_id)

    @staticmethod
    def _scan_result(
        posts: list[Post],
        scrolls: int,
        bounded: bool,
        boundary_post_id: str | None,
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
            LOGGER.warning(
                "Mobile post order uncertain",
                missing_components="publication_time",
                post_count=len(posts),
            )
        posts = [replace(post, position=index) for index, post in enumerate(posts)]
        marker_found = any(post.post_id == boundary_post_id for post in posts)
        if boundary_post_id and (not marker_found or bounded):
            LOGGER.warning(
                "Mobile catch-up coverage incomplete",
                marker_found=marker_found,
                post_count=len(posts),
                scroll_count=scrolls,
                bounded=bounded,
            )
        return ScanResult(tuple(posts), PageState.FEED.value, scrolls, bounded)
