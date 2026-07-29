"""Headless browser checks against sanitized local DOM fixtures only."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fbn.browser import (
    PageState,
    PlaywrightPostSource,
    classify_page,
    collect_dom_payloads,
    collect_group_aliases,
    read_page_signals,
    wait_for_terminal_page,
)
from fbn.config import BrowserSettings
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    BrowserUnavailableError,
    LayoutChangedError,
    TransientNavigationError,
)
from fbn.extractor import extract_posts, parse_group_ref

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@contextmanager
def _local_context(tmp_path: Path) -> Iterator[object]:
    requested = os.environ.get("FBN_TEST_BROWSER", "chromium")
    settings = BrowserSettings(
        browser=requested,
        profile_dir=tmp_path / requested,
        headless=True,
    )
    source = PlaywrightPostSource(settings)
    try:
        with source._context(headless=True) as context:
            yield context
            return
    except BrowserUnavailableError:
        if "FBN_TEST_BROWSER" in os.environ or sys.platform != "darwin":
            pytest.fail(f"the required {requested} headless browser did not launch")

    # Local macOS development can use installed Chrome when the matching
    # Playwright Chromium binary has not been downloaded. CI always exercises
    # the explicit Chromium path above.
    fallback = PlaywrightPostSource(
        BrowserSettings(
            browser="chrome",
            profile_dir=tmp_path / "chrome",
            headless=True,
        )
    )
    try:
        with fallback._context(headless=True) as context:
            yield context
    except BrowserUnavailableError:
        pytest.skip("no local Playwright Chromium or Chrome browser is installed")


def test_headless_browser_classifies_all_sanitized_page_states(
    tmp_path: Path,
) -> None:
    expected_errors = {
        "login.html": AuthenticationRequiredError,
        "checkpoint.html": AccountActionRequiredError,
        "captcha.html": AccountActionRequiredError,
        "blocked.html": TransientNavigationError,
        "body_blocked.html": TransientNavigationError,
        "access_denied.html": AccessDeniedError,
        "body_access_denied.html": AccessDeniedError,
        "layout_changed.html": LayoutChangedError,
    }

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("feed.html"))
            assert classify_page(read_page_signals(page)) is PageState.FEED

            page.set_content(_fixture("empty.html"))
            assert classify_page(read_page_signals(page)) is PageState.EMPTY

            for fixture_name, error_type in expected_errors.items():
                page.set_content(_fixture(fixture_name))
                with pytest.raises(error_type):
                    classify_page(read_page_signals(page))
        finally:
            page.close()


def test_headless_browser_extracts_ordered_visible_posts(tmp_path: Path) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("feed.html"))
            payloads = collect_dom_payloads(page)
        finally:
            page.close()

    posts = extract_posts(payloads, group, observed_at, limit=10)

    assert len(payloads) == 2
    assert [post.post_id for post in posts] == ["101", "pfbidExample12345"]
    assert all(post.post_id != "999" for post in posts)
    assert [post.author for post in posts] == ["Alice Example", "Bob Example"]
    assert "First visible post body" in posts[0].text
    assert posts[0].url == ("https://www.facebook.com/groups/test-group/posts/101/")
    assert posts[1].partial is True


def test_headless_browser_accepts_one_article_inside_positioned_feed_item(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("positioned_wrapper_feed.html"))
            payloads = collect_dom_payloads(page)
        finally:
            page.close()

    posts = extract_posts(payloads, group, observed_at, limit=10)

    assert len(payloads) == 1
    assert [post.post_id for post in posts] == ["201"]
    assert all(post.post_id != "999" for post in posts)
    assert posts[0].author == "Alice Example"
    assert "Visible post inside a positioned feed wrapper" in posts[0].text


@pytest.mark.parametrize("fixture_name", ["sidebar_only.html", "nested_only.html"])
def test_headless_browser_rejects_links_outside_direct_feed_items(
    tmp_path: Path,
    fixture_name: str,
) -> None:
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture(fixture_name))
            payloads = collect_dom_payloads(page)
            signals = read_page_signals(page)
        finally:
            page.close()

    assert payloads == []
    with pytest.raises(LayoutChangedError):
        classify_page(signals)


def test_headless_browser_accepts_one_group_header_alias(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("1663189947098862")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("alias_feed.html"))
            payloads = collect_dom_payloads(page)
            aliases = collect_group_aliases(page, group)
        finally:
            page.close()

    posts = extract_posts(
        payloads,
        group,
        datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        limit=10,
        allowed_group_keys=aliases,
    )
    assert aliases == frozenset({"1663189947098862", "custom-alias"})
    assert len(posts) == 1
    assert posts[0].group_key == "1663189947098862"


def test_headless_browser_rejects_unrelated_group_link_as_alias(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("1663189947098862")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("related_group_feed.html"))
            payloads = collect_dom_payloads(page)
            aliases = collect_group_aliases(page, group)
        finally:
            page.close()

    posts = extract_posts(
        payloads,
        group,
        datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        limit=10,
        allowed_group_keys=aliases,
    )
    assert aliases == frozenset({"1663189947098862"})
    assert posts == ()


def test_headless_browser_waits_through_a_dynamic_feed_skeleton(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(
                '<main role="main"><div id="feed" role="feed"></div></main>'
            )
            page.evaluate(
                """
                () => setTimeout(() => {
                  const article = document.createElement('article');
                  article.setAttribute('role', 'article');
                  article.innerHTML =
                    '<a href="/groups/test-group/posts/123/">post</a>';
                  document.querySelector('#feed').appendChild(article);
                }, 100)
                """
            )

            state = wait_for_terminal_page(page, timeout_seconds=2)
        finally:
            page.close()

    assert state is PageState.FEED


def test_headless_browser_does_not_false_empty_before_item_link_hydrates(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(
                """
                <main role="main">
                  <div id="feed" role="feed">
                    <article id="item" role="article">no posts yet</article>
                  </div>
                </main>
                """
            )
            page.evaluate(
                """
                () => setTimeout(() => {
                  const link = document.createElement('a');
                  link.href = '/groups/test-group/posts/124/';
                  link.innerText = 'hydrated permalink';
                  document.querySelector('#item').appendChild(link);
                }, 100)
                """
            )

            state = wait_for_terminal_page(page, timeout_seconds=2)
        finally:
            page.close()

    assert state is PageState.FEED


def test_persistent_profile_retains_facebook_cookie_across_headless_relaunch(
    tmp_path: Path,
) -> None:
    settings = BrowserSettings(
        browser=os.environ.get("FBN_TEST_BROWSER", "chromium"),
        profile_dir=tmp_path / "persistent-cookie-profile",
        headless=True,
    )
    source = PlaywrightPostSource(settings)
    cookie = {
        "name": "fbn_persistence_probe",
        "value": "local-sanitized-value",
        "domain": ".facebook.com",
        "path": "/",
        "expires": 2_000_000_000,
        "secure": True,
    }
    try:
        with source._context(headless=True) as context:
            context.add_cookies([cookie])
        with source._context(headless=True) as context:
            persisted = context.cookies("https://www.facebook.com/")
    except BrowserUnavailableError:
        if "FBN_TEST_BROWSER" in os.environ or sys.platform != "darwin":
            pytest.fail("the required headless browser did not launch")
        pytest.skip("no local Playwright Chromium browser is installed")

    assert any(
        item["name"] == cookie["name"] and item["value"] == cookie["value"]
        for item in persisted
    )
