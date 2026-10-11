"""Headless browser checks against sanitized local DOM fixtures only."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pytest

from fbn.browser import (
    PageState,
    PlaywrightPostSource,
    classify_page,
    collect_dom_payloads,
    collect_group_aliases,
    hydrate_post_permalinks,
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
from fbn.mobile import (
    MobilePostSource,
    copy_post_link,
    mobile_records,
    resolve_post_link,
    select_new_posts,
    wait_for_mobile_page,
)
from fbn.models import ScanPolicy
from fbn.state import SQLiteStateRepository
from tests.mobile_fixtures import mobile_fixture

FIXTURES = Path(__file__).parent / "fixtures"


# These assertions describe reader behavior, not desktop selectors. Register
# the mobile adapter here only after this contract passes against desktop.
@pytest.fixture(params=["desktop", "mobile"])
def reader_contract(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[object]:
    view = request.param
    group = parse_group_ref("contract-group")
    observed_at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def read(stories: list[tuple[str, str, str]]) -> tuple:
            if view == "mobile":
                by_id = {story[0]: story for story in stories}

                def route_mobile(route):
                    path = route.request.url.split("?")[0].rstrip("/").split("/")
                    is_post = "share" in path or "posts" in path
                    chosen = [by_id[path[-1]]] if is_post else stories
                    html = mobile_fixture(chosen)
                    if "share" in path:
                        # Playwright routes only the first request of an HTTP
                        # redirect chain. Simulate its final URL locally so the
                        # fixture never attempts the redirected network request.
                        html += (
                            "<script>history.replaceState(null,'','/groups/"
                            f"{group.key}/posts/{path[-1]}/')</script>"
                        )
                    route.fulfill(status=200, content_type="text/html", body=html)

                context.route("**/*", route_mobile)
                page.goto(f"https://m.facebook.com/groups/{group.key}/")
                target = context.new_page()
                payloads = []
                try:
                    with mobile_records(page) as records:
                        for menu, payload in records:
                            copied = copy_post_link(page, menu, timeout_seconds=1)
                            href = resolve_post_link(
                                target,
                                copied,
                                payload,
                                frozenset({group.key}),
                                timeout_seconds=1,
                            )
                            payloads.append({**payload, "href": href})
                    return extract_posts(payloads, group, observed_at, limit=50)
                finally:
                    target.close()
                    context.unroute("**/*", route_mobile)
            assert view == "desktop"
            cards = []
            for position, (post_id, author, body) in enumerate(stories, start=1):
                cards.append(
                    f'<article role="article" aria-posinset="{position}">'
                    f'<h2><a href="https://www.facebook.com/author">'
                    f"{escape(author)}</a></h2>"
                    '<div data-ad-rendering-role="story_message">'
                    f"{escape(body)}</div>"
                    f'<a href="{group.url}posts/{post_id}/?ref=share">2h</a>'
                    '<article role="article" aria-label="Shared post">'
                    '<h2><a href="https://www.facebook.com/quoted">Quoted</a></h2>'
                    f'<a href="{group.url}posts/999/">Quoted body</a></article>'
                    '<div aria-label="Comment by Reply Example">'
                    '<a href="https://www.facebook.com/reply">Reply Example</a>'
                    f'<a href="{group.url}posts/998/?comment_id=7">'
                    "Nested reply body</a></div></article>"
                )
            page.set_content(
                '<main><div role="feed">' + "".join(cards) + "</div></main>"
            )
            return extract_posts(
                collect_dom_payloads(page), group, observed_at, limit=50
            )

        try:
            yield read
        finally:
            page.close()


def test_reader_contract_main_post_content_and_link_stay_together(reader_contract):
    posts = reader_contract(
        [("301", "First Author", "First body"), ("302", "Second Author", "Second body")]
    )
    assert [(post.post_id, post.author, post.text) for post in posts] == [
        ("301", "First Author", "First body"),
        ("302", "Second Author", "Second body"),
    ]
    for post in posts:
        assert post.url.endswith(f"/posts/{post.post_id}/")
        assert post.fallback_key is not None
        assert "Reply" not in post.text
        assert "Quoted" not in post.text


def test_reader_contract_duplicate_cards_do_not_duplicate_posts(reader_contract):
    posts = reader_contract(
        [("301", "First Author", "First body"), ("301", "First Author", "First body")]
    )
    assert [post.post_id for post in posts] == ["301"]


def test_reader_contract_catches_every_post_since_marker(reader_contract, tmp_path):
    group = parse_group_ref("contract-group")
    with SQLiteStateRepository(tmp_path / "contract.sqlite3") as state:
        baseline = state.observe(
            group, reader_contract([("300", "Author", "Previous scan")])
        )
        assert baseline.baseline and not baseline.pending
        batch = state.observe(
            group,
            reader_contract(
                [
                    ("302", "Author", "Newest post"),
                    ("301", "Author", "Post missed between checks"),
                    ("300", "Author", "Previous scan"),
                ]
            ),
        )
        assert {item.post_id for item in batch.pending} == {"301", "302"}


def test_reader_contract_missing_marker_keeps_gap_open(reader_contract, tmp_path):
    group = parse_group_ref("contract-group")
    with SQLiteStateRepository(tmp_path / "contract.sqlite3") as state:
        state.observe(group, reader_contract([("300", "Author", "Previous scan")]))
        first = state.observe(group, reader_contract([("302", "Author", "Newest")]))
        state.mark_delivered([item.event_id for item in first.pending])
        assert state.notification_boundary(group) == "300"
        later = state.observe(
            group,
            reader_contract(
                [
                    ("303", "Author", "Newer"),
                    ("302", "Author", "Newest"),
                    ("301", "Author", "Only exposed on a later check"),
                ]
            ),
        )
        assert {item.post_id for item in later.pending} == {"301", "303"}
        assert state.notification_boundary(group) == "300"


def test_reader_contract_restart_does_not_repeat_delivered_posts(
    reader_contract, tmp_path
):
    group = parse_group_ref("contract-group")
    path = tmp_path / "contract.sqlite3"
    stories = [("301", "Author", "Already delivered")]
    with SQLiteStateRepository(path) as state:
        batch = state.observe(group, reader_contract(stories), notify_initial=True)
        state.mark_delivered([item.event_id for item in batch.pending])
    with SQLiteStateRepository(path) as state:
        repeated = state.observe(group, reader_contract(stories))
        assert repeated.inserted == repeated.queued == 0
        assert not repeated.pending


def test_reader_contract_identical_content_can_belong_to_distinct_posts(
    reader_contract,
):
    posts = reader_contract(
        [
            ("301", "Author", "Repeated announcement"),
            ("302", "Author", "Repeated announcement"),
        ]
    )
    assert [post.post_id for post in posts] == ["301", "302"]


def test_mobile_selects_new_posts_instead_of_trusting_the_url(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Author", "Body")], sorted_feed=False))
        select_new_posts(page, timeout_seconds=1)
        assert page.locator("#sort-label").inner_text() == "New posts"
        assert not page.locator("#sort-menu").is_visible()
        assert wait_for_mobile_page(page, timeout_seconds=1) is PageState.FEED


def test_mobile_excludes_discussion_nested_inside_the_body_text_component(tmp_path):
    html = mobile_fixture([("301", "Author", "Primary body")]).replace(
        ">Primary body</div>",
        '>Primary body<span aria-label="Comment by Another">Hidden reply</span></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["text"] == "Primary body"


@pytest.mark.parametrize("hydrated", [False, True])
@pytest.mark.parametrize("comment_label", ["\U000f0379 2comments", "\U000f0379comment"])
def test_mobile_reads_hydrated_text_and_icon_prefixed_actions(
    tmp_path, hydrated, comment_label
):
    html = mobile_fixture([("301", "Author", "Primary body")], sorted_feed=False)
    if hydrated:
        html = html.replace("ServerTextArea", "TextArea").replace(
            "ServerImageArea", "ImageArea"
        )
    html = html.replace(
        "Like, press and hold for more reactions",
        "2like, double-tap and hold for more reactions",
    ).replace("Make a comment", comment_label)
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        select_new_posts(page, timeout_seconds=1)
        assert wait_for_mobile_page(page, timeout_seconds=1) is PageState.FEED
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["author"] == "Author"
            assert records[0][1]["text"] == "Primary body"


def test_mobile_reply_actions_cannot_replace_the_primary_footer(tmp_path):
    html = mobile_fixture([("301", "Author", "Primary body")]).replace(
        'aria-label="Make a comment"', 'aria-label="Reply"'
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="primary_action_footer"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_waits_for_all_primary_cards_to_finish_loading(tmp_path):
    html = mobile_fixture([("301", "Author", "Body"), ("302", "Other", "Body")])
    html = html.replace(
        'aria-label="More options for Other"',
        'aria-label="More options for Other" id="loading-menu"',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate("""() => {
            const timestamp = document.getElementById('loading-menu').parentElement
                .querySelector('[data-mcomponent=ServerTextArea][aria-label]');
            const label = timestamp.getAttribute('aria-label');
            timestamp.removeAttribute('aria-label');
            setTimeout(() => timestamp.setAttribute('aria-label', label), 100);
        }""")
        assert wait_for_mobile_page(page, timeout_seconds=1) is PageState.FEED


def test_mobile_waits_for_scroller_hydration_without_reloading(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        'data-type="vscroller"', 'data-type="loading" id="loading-scroller"'
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate("""() => {
            setTimeout(() => document.getElementById('loading-scroller')
                .setAttribute('data-type', 'vscroller'), 350);
        }""")
        assert wait_for_mobile_page(page, timeout_seconds=1) is PageState.FEED
        with mobile_records(page) as records:
            assert len(records) == 1


def test_missing_mobile_scroller_logs_structure_without_page_content(
    tmp_path, monkeypatch
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type("Log", (), {"warning": lambda self, message, **kw: logged.append(kw)})(),
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            '<div data-mcomponent="MScreen"><div role="progressbar"></div>'
            "Synthetic private body must not appear in diagnostics</div>"
        )
        with pytest.raises(LayoutChangedError, match="unique_mobile_scroller"):
            wait_for_mobile_page(page, timeout_seconds=0.05, permalink=True)
    snapshot = logged[0]
    assert snapshot["page_kind"] == "permalink"
    assert snapshot["mobile_screens"] == snapshot["loading_indicators"] == 1
    assert snapshot["total_scrollers"] == snapshot["visible_scrollers"] == 0
    assert "Synthetic private body" not in str(logged)
    assert "facebook.com" not in str(logged)


def test_mobile_defers_unrendered_cards_until_scrolling_makes_them_readable(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture([("301", "First", "Body"), ("302", "Second", "Later body")])
        )
        card = page.locator('[data-story="302"]')
        card.evaluate("""n => {
            n.style.marginTop = '10000px';
            n.style.contentVisibility = 'auto';
            n.style.containIntrinsicSize = '500px';
        }""")
        page.wait_for_timeout(100)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            assert [payload["author"] for _, payload in records] == ["First"]
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(100)
        assert wait_for_mobile_page(page, timeout_seconds=1) is PageState.FEED
        with mobile_records(page) as records:
            assert [payload["author"] for _, payload in records] == ["First", "Second"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_mobile_verifies_permalink_with_author_inside_timestamp_component(
    tmp_path, wrapped
):
    html = mobile_fixture([("301", "Author", "Body")])
    target_html = html.replace('<span role="link">Author</span>', "Group title")
    target_html = target_html.replace(
        'aria-label="Sunday, October 11, 2026 at 10:00 AM"',
        'aria-label="Author, Sunday, October 11, 2026 at 10:00 AM"',
    ).replace(">2h</div>", '><span role="link">Author</span> 2h</div>')
    if not wrapped:
        target_html += """<script>
          const root = document.querySelector('[data-type=vscroller]');
          root.firstElementChild.remove();
          const card = root.querySelector('[data-story]');
          card.firstElementChild.remove();
          card.replaceWith(...card.children);
          root.prepend(document.createElement('div'), document.createElement('div'));
        </script>"""
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=1,
        ).endswith("/posts/301/")


@pytest.mark.parametrize(
    "badge",
    ["All-star contributor", "Top contributor", "Rising contributor", "Group expert"],
)
@pytest.mark.parametrize("permalink", [False, True])
def test_mobile_reads_timestamp_from_contributor_metadata_wrapper(
    tmp_path, badge, permalink
):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        '<div data-mcomponent="MContainer" '
        f'aria-label="{badge}, , Sunday, October 11, 2026 at 10:00 AM, Private group">'
        f'<div data-mcomponent="TextArea">{badge}</div>'
        '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert (
            wait_for_mobile_page(page, timeout_seconds=0.1, permalink=permalink)
            is PageState.FEED
        )
        with mobile_records(page, permalink=permalink) as records:
            assert len(records) == 1
            assert records[0][1]["timestampLabel"] == (
                "Sunday, October 11, 2026 at 10:00 AM, Private group"
            )


@pytest.mark.parametrize("badge", [None, "All-star contributor"])
@pytest.mark.parametrize("include_author", [False, True])
def test_mobile_reads_primary_date_from_metadata_container(
    tmp_path, badge, include_author
):
    prefix = "Author, " if include_author else ""
    prefix += f"{badge}, , " if badge else ""
    contents = '<span role="link">Author</span>' if include_author else ""
    contents += f'<div data-mcomponent="TextArea">{badge}</div>' if badge else ""
    html = mobile_fixture([("301", "Author", "Body")])
    if include_author:
        html = html.replace('<span role="link">Author</span>', "Group title")
    html = html.replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        '<div data-mcomponent="MContainer" '
        f'aria-label="{prefix}Sunday, October 11, 2026 at 10:00 AM, Private group">'
        + contents
        + '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert (
            wait_for_mobile_page(page, timeout_seconds=0.1, permalink=True)
            is PageState.FEED
        )
        with mobile_records(page, permalink=True) as records:
            assert len(records) == 1
            assert records[0][1]["timestampLabel"] == (
                "Sunday, October 11, 2026 at 10:00 AM, Private group"
            )


@pytest.mark.parametrize(
    "prefix_order",
    ["badge-author", "badge-author-badge", "author-badge", "badge-badge-author"],
)
@pytest.mark.parametrize(
    "date", ["4 days ago", "26 Sep", "Sunday, October 11, 2026 at 10:00 AM"]
)
def test_mobile_verifies_metadata_with_badges_before_or_after_author(
    tmp_path, prefix_order, date
):
    source_html = mobile_fixture([("301", "Primary Author", "Primary body")]).replace(
        "Sunday, October 11, 2026 at 10:00 AM", date
    )
    parts = prefix_order.split("-")
    badges = iter(["All-star contributor", "Group expert"])
    labels = ["Primary Author" if part == "author" else next(badges) for part in parts]
    contents = "".join(
        '<div data-mcomponent="ServerTextArea">'
        + ('<span role="link">Primary Author</span>' if part == "author" else label)
        + "</div>"
        for part, label in zip(parts, labels, strict=True)
    )
    target_html = source_html.replace(
        '<span role="link">Primary Author</span>', "Group title"
    ).replace(
        '<div data-mcomponent="ServerTextArea" ' + f'aria-label="{date}">2h</div>',
        '<div data-mcomponent="MContainer" '
        + f'aria-label="{", , ".join(labels)}, {date}, Private group">'
        + contents
        + '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        source = context.new_page()
        source.set_content(source_html)
        with mobile_records(source) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=2,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("wrapper", [False, True])
def test_mobile_verifies_older_post_with_date_only_publication(tmp_path, wrapper):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h',
        'aria-label="26 Sep, Private group">26 Sep',
    )
    if wrapper:
        html = html.replace(
            '<div data-mcomponent="ServerTextArea" '
            'aria-label="26 Sep, Private group">26 Sep</div>',
            '<div data-mcomponent="MContainer" '
            'aria-label="Group expert, , 26 Sep, Private group">'
            '<div data-mcomponent="TextArea">Group expert</div>'
            '<div data-mcomponent="TextArea">26 Sep</div></div>',
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=1,
        ).endswith("/posts/301/")


def test_mobile_ignores_non_date_accessibility_labels_in_primary_header(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<span role="link">Author</span>',
        '<span role="link">Author</span>'
        '<div data-mcomponent="TextArea" aria-label="Group expert">Group expert</div>'
        '<div data-mcomponent="TextArea" aria-label="Follow">Follow</div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            assert len(records) == 1


def test_mobile_waits_for_short_link_client_side_canonical_navigation(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")])
    target_html = (
        html
        + """<script>
      setTimeout(() => {
        history.replaceState(null, '', '/groups/contract-group/posts/301/');
      }, 100);
    </script>"""
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/share/p/example/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=1,
        ).endswith("/posts/301/")


@pytest.mark.parametrize(
    "invalid",
    ["badge_only", "unrelated_badge", "two_dates", "menu_container", "foreign_author"],
)
def test_mobile_contributor_wrapper_cannot_hide_missing_or_ambiguous_timestamp(
    tmp_path, invalid
):
    replacement = (
        '<div data-mcomponent="MContainer" '
        'aria-label="All-star contributor, , 2 hours ago, Private group">'
        '<div data-mcomponent="TextArea">All-star contributor</div>'
        '<div data-mcomponent="TextArea">2h</div></div>'
    )
    if invalid == "badge_only":
        replacement = replacement.replace(", , 2 hours ago, Private group", "")
    elif invalid == "unrelated_badge":
        replacement = replacement.replace(">All-star contributor<", ">Another badge<")
    elif invalid == "two_dates":
        replacement += (
            '<div data-mcomponent="TextArea" aria-label="3 hours ago">3h</div>'
        )
    elif invalid == "foreign_author":
        replacement = replacement.replace(
            "All-star contributor, ,", "Another,"
        ).replace(">All-star contributor<", '><a role="link">Another</a><')
    else:
        replacement = replacement.replace(
            ">2h</div>",
            '>2h<button role="button" aria-label="More options for Nested">'
            "...</button></div>",
        )
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        replacement,
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="unique_primary_"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_does_not_accept_sort_text_inside_a_post(tmp_path):
    html = mobile_fixture([("301", "Author", "New posts")], sorted_feed=False)
    html = html.replace('id="sort-label"', 'id="sort-label" style="display:none"')
    html = html.replace(">\u200bSort\u200b<", ">Unavailable<")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="group_sort_control"):
            select_new_posts(page, timeout_seconds=0.1)


def test_mobile_failed_copy_cleans_up_and_cannot_reuse_a_previous_link(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        "function copyPost() {navigator.clipboard.writeText(",
        "function copyPost() {return; navigator.clipboard.writeText(",
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate(
            "window.__fbnPreviousCopiedLink='https://m.facebook.com/share/p/999/'"
        )
        with mobile_records(page) as records:
            assert len(records) == 1
            with pytest.raises(Exception, match="Timeout"):
                copy_post_link(page, records[0][0], timeout_seconds=0.1)
        assert not page.locator("#post-menu").is_visible()
        assert page.evaluate("!window.__fbnCopyCapture")
        assert page.evaluate("!Object.hasOwn(navigator,'clipboard')")


@pytest.mark.parametrize("missing", ["author", "footer", "timestamp"])
def test_mobile_incomplete_primary_card_fails_closed(tmp_path, missing):
    html = mobile_fixture([("301", "Author", "Body")])
    replacements = {
        "author": ('<span role="link">Author</span>', "<span>Author</span>"),
        "footer": ('aria-label="Make a comment"', 'aria-label="Other action"'),
        "timestamp": ('aria-label="Sunday, October 11, 2026 at 10:00 AM"', ""),
    }
    html = html.replace(*replacements[missing])
    if missing == "footer":
        html = html.replace(
            'aria-label="Like, press and hold for more reactions"',
            'aria-label="Other action"',
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="missing="):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    "wrong", ["body", "author", "group", "comment_only", "offsite", "timestamp"]
)
def test_mobile_rejects_wrong_copied_link_destinations(tmp_path, wrong):
    source_payload = {
        "author": "Author",
        "text": "Expected body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    stories = [("999", "Author", "Expected body")]
    if wrong in {"body", "comment_only"}:
        stories = [("999", "Author", "Different primary body")]
    if wrong == "author":
        stories = [("999", "Different Author", "Expected body")]
    with _local_context(tmp_path) as context:
        page = context.new_page()
        html = mobile_fixture(stories)
        if wrong == "comment_only":
            html = html.replace("Nested reply body", "Expected body")
        if wrong == "timestamp":
            html = html.replace("at 10:00 AM", "at 9:00 AM")
        group_key = "wrong-group" if wrong == "group" else "contract-group"

        def route_target(route):
            body = (
                html
                + "<script>history.replaceState(null,'','/groups/"
                + f"{group_key}/posts/999/')</script>"
            )
            route.fulfill(status=200, content_type="text/html", body=body)

        context.route("**/*", route_target)
        copied = (
            "https://example.invalid/share/p/999/"
            if wrong == "offsite"
            else "https://m.facebook.com/share/p/999/"
        )
        with pytest.raises(LayoutChangedError):
            resolve_post_link(
                page,
                copied,
                source_payload,
                frozenset({"contract-group"}),
                timeout_seconds=2,
            )


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("uppercase_sort", [False, True])
def test_mobile_complete_fetch_is_bounded_and_reads_beyond_the_marker(
    tmp_path, rename, uppercase_sort
):
    group = parse_group_ref("123" if rename else "contract-group")
    stories = [("302", "Author", "Newer"), ("301", "Author", "Marker")]
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        opened = []
        context.on("page", lambda page: opened.append(page))

        def routes(route):
            url = route.request.url
            key = url.split("?")[0].rstrip("/").split("/")[-1]
            is_post = "/share/" in url or "/posts/" in url
            chosen = (
                [next(story for story in stories if story[0] == key)]
                if is_post
                else stories
            )
            html = mobile_fixture(
                chosen, sorted_feed=is_post, uppercase_sort=uppercase_sort
            )
            if rename and not is_post:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/')</script>"
                )
            if "/share/" in url:
                html += (
                    "<script>history.replaceState(null,'','/groups/"
                    f"contract-group/posts/{key}/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        result = source.fetch_recent(
            group,
            ScanPolicy(sample_count=1, max_scrolls=2, settle_seconds=0),
            boundary_post_id="301",
        )
        assert [post.post_id for post in result.posts] == ["302", "301"]
        assert result.scrolls >= 1
        assert not result.bounded
        assert tuple(context.pages) == original_pages
        # The feed and each verified post own a separate, promptly closed tab.
        assert len(opened) == 3
        assert all(page.is_closed() for page in opened)


def test_mobile_fetch_allows_post_menu_to_load_beyond_old_three_second_cap(tmp_path):
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)

        def routes(route):
            html = mobile_fixture([("301", "Author", "Body")])
            if "/share/" in route.request.url:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            else:
                html = html.replace(
                    'document.getElementById("post-menu").hidden=false;',
                    'setTimeout(() => document.getElementById("post-menu")'
                    ".hidden=false, 3200);",
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        result = source.fetch_recent(
            parse_group_ref("contract-group"),
            ScanPolicy(sample_count=1, max_scrolls=0, navigation_timeout_seconds=5),
        )
        assert [post.post_id for post in result.posts] == ["301"]
        assert tuple(context.pages) == original_pages


def test_scan_recovers_hover_only_primary_post_permalink(tmp_path: Path) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    source = PlaywrightPostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=_fixture("hover_permalink_feed.html"),
            ),
        )
        page.goto(group.url)
        before = extract_posts(collect_dom_payloads(page), group, observed_at, 10)
        result = source._scan_feed(page, group, ScanPolicy(max_scrolls=0), observed_at)
        assert page.locator("#timestamp").get_attribute("data-hovered") == "yes"
        assert page.locator("#comment-timestamp").get_attribute("data-hovered") is None
        assert page.locator("#quoted-timestamp").get_attribute("data-hovered") is None

    assert len(before) == len(result.posts) == 1
    assert before[0].url == group.url
    recovered = result.posts[0]
    assert recovered.url == "https://www.facebook.com/groups/test-group/posts/502/"
    assert recovered.author == before[0].author == "Primary Example"
    assert recovered.text == before[0].text == "Primary story with photos"
    assert recovered.fallback_key == before[0].fallback_key
    assert recovered.fallback_key is not None

    with SQLiteStateRepository(tmp_path / "state.sqlite3") as state:
        first = state.observe(group, before, notify_initial=True)
        state.mark_delivered([item.event_id for item in first.pending])
        next_observation = state.observe(group, result.posts)
        assert next_observation.inserted == next_observation.queued == 0
        assert next_observation.reconciled == 1
        assert not next_observation.pending
        row = (
            state._require_connection()
            .execute("SELECT canonical_url FROM posts")
            .fetchone()
        )
        assert row["canonical_url"] == recovered.url


@pytest.mark.parametrize(
    "href",
    [
        "https://www.facebook.com/groups/test-group/posts/502/",
        "https://example.invalid/?__tn__=%2CO",
    ],
)
def test_hydration_skips_direct_and_offsite_timestamps(
    tmp_path: Path, href: str
) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    fixture = _fixture("hover_permalink_feed.html").replace(
        'id="timestamp" href="https://www.facebook.com/groups/test-group/?__tn__=%2CO"',
        f'id="timestamp" href="{href}"',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=fixture
            ),
        )
        page.goto(group.url)
        before = extract_posts(collect_dom_payloads(page), group, observed_at, 10)
        hydrate_post_permalinks(page)
        after = extract_posts(collect_dom_payloads(page), group, observed_at, 10)
        assert page.locator("#timestamp").get_attribute("data-hovered") is None
        assert page.locator("#comment-timestamp").get_attribute("data-hovered") is None
        assert page.locator("#quoted-timestamp").get_attribute("data-hovered") is None

    assert before == after
    assert len(after) == 1
    assert after[0].text == "Primary story with photos"
    assert after[0].fallback_key is not None


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@contextmanager
def _local_context(
    tmp_path: Path,
    *,
    timezone_id: str | None = None,
) -> Iterator[object]:
    requested = os.environ.get("FBN_TEST_BROWSER", "chromium")
    settings = BrowserSettings(
        browser=requested,
        profile_dir=tmp_path / requested,
        headless=True,
    )
    source = PlaywrightPostSource(settings)
    try:
        with source._context(
            headless=True,
            timezone_id=timezone_id,
        ) as context:
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
        with fallback._context(
            headless=True,
            timezone_id=timezone_id,
        ) as context:
            yield context
    except BrowserUnavailableError:
        pytest.skip("no local Playwright Chromium or Chrome browser is installed")


def test_headless_browser_uses_configured_timezone(tmp_path: Path) -> None:
    with _local_context(tmp_path, timezone_id="America/New_York") as context:
        page = context.new_page()
        try:
            timezone_id = page.evaluate(
                "() => Intl.DateTimeFormat().resolvedOptions().timeZone"
            )
        finally:
            page.close()

    assert timezone_id == "America/New_York"


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
    assert posts[0].published_at == observed_at - timedelta(minutes=2)
    assert posts[1].published_at == observed_at - timedelta(minutes=5)
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
    assert not posts[0].text.startswith("Facebook")
    assert not posts[0].text.endswith("Facebook")
    assert "Follow" not in posts[0].text
    assert "metadata" not in posts[0].text
    assert "Visible post inside a positioned feed wrapper" in posts[0].text


def test_headless_browser_extracts_photo_only_group_post(tmp_path: Path) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("photo_feed.html"))
            payloads = collect_dom_payloads(page)
        finally:
            page.close()

    posts = extract_posts(payloads, group, observed_at, limit=10)

    assert len(payloads) == 1
    assert [post.post_id for post in posts] == ["202"]
    assert posts[0].url == "https://www.facebook.com/groups/test-group/posts/202/"
    assert posts[0].author == "Alice Example"
    assert posts[0].text.startswith("Fresh produce!!")
    assert "Follow" not in posts[0].text
    assert "contributor" not in posts[0].text
    assert "decoy" not in posts[0].text
    assert "41m" not in posts[0].text
    assert "\u034f" not in posts[0].text
    assert payloads[0]["timestamp"] == "41m"
    assert posts[0].published_at == observed_at - timedelta(minutes=41)


def test_headless_browser_excludes_nested_reply_author_and_body(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("comment_reply_feed.html"))
            payloads = collect_dom_payloads(page)
        finally:
            page.close()

    posts = extract_posts(payloads, group, observed_at, limit=10)

    assert len(posts) == 1
    assert posts[0].post_id == "301"
    assert posts[0].author == "Parent Example"
    assert posts[0].text == "Primary post body"
    assert "Reply Example" not in posts[0].text
    assert "Nested reply body" not in posts[0].text


def test_headless_browser_fingerprints_post_when_permalink_is_omitted(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("no_permalink_feed.html"))
            first_payloads = collect_dom_payloads(page)
            second_payloads = collect_dom_payloads(page)
            signals = read_page_signals(page)
        finally:
            page.close()

    first = extract_posts(first_payloads, group, observed_at, limit=10)
    second = extract_posts(second_payloads, group, observed_at, limit=10)

    assert classify_page(signals) is PageState.FEED
    assert signals.feed_root_count == 1
    assert signals.top_level_item_count == 1
    assert signals.direct_permalink_count == 0
    assert signals.story_message_count == 1
    assert signals.fallback_post_count == 1
    assert signals.author_count == 1
    assert len(first) == 1
    assert first[0].post_id.startswith("content-")
    assert first[0].post_id == second[0].post_id
    assert first[0].fallback_key == second[0].fallback_key
    assert first[0].fallback_key is not None
    assert first[0].url == ("https://www.facebook.com/groups/test-group/posts/999/")
    assert first[0].author == "Parent Example"
    assert first[0].text == "Primary post without a permalink"
    assert "Reply Example" not in first[0].text
    assert "Nested reply body" not in first[0].text
    assert first[0].published_at is None


def test_headless_browser_recovers_fallback_url_from_group_photo_set(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("test-group")
    observed_at = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)

    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("photo_without_group_feed.html"))
            payloads = collect_dom_payloads(page)
        finally:
            page.close()

    posts = extract_posts(payloads, group, observed_at, limit=10)

    assert len(posts) == 1
    assert posts[0].post_id.startswith("content-")
    assert posts[0].fallback_key is not None
    assert posts[0].url == "https://www.facebook.com/groups/test-group/posts/202/"
    assert posts[0].author == "Photo Author"
    assert posts[0].text == "Photo post without an idorvanity parameter"


def test_headless_browser_owns_story_outside_comment_articles(tmp_path: Path) -> None:
    group = parse_group_ref("test-group")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("wrapper_story_feed.html"))
            payloads = collect_dom_payloads(page)
            signals = read_page_signals(page)
        finally:
            page.close()
    posts = extract_posts(
        payloads, group, datetime(2026, 10, 1, tzinfo=timezone.utc), limit=10
    )
    assert classify_page(signals) is PageState.FEED
    assert signals.author_count == signals.story_message_count == 1
    assert signals.direct_permalink_count == 0
    assert len(posts) == 1
    assert posts[0].author == "Primary Example"
    assert posts[0].text == "Primary wrapper story"
    assert posts[0].url == "https://www.facebook.com/groups/test-group/posts/501/"
    assert posts[0].post_id.startswith("content-")


def test_wrapper_story_does_not_use_quoted_or_nested_reply_links(
    tmp_path: Path,
) -> None:
    group = parse_group_ref("test-group")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        try:
            page.set_content(_fixture("wrapper_story_feed.html"))
            page.locator('a[href*="comment_id=42"]').evaluate(
                "element => element.remove()"
            )
            payloads = collect_dom_payloads(page)
        finally:
            page.close()
    posts = extract_posts(
        payloads, group, datetime(2026, 10, 1, tzinfo=timezone.utc), limit=10
    )
    assert len(posts) == 1
    assert posts[0].text == "Primary wrapper story"
    assert posts[0].url == group.url


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
