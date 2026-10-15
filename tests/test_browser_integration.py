"""Headless browser checks against sanitized local DOM fixtures only."""

from __future__ import annotations

import json
import os
import stat
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fbn.browser import PageState
from fbn.config import BrowserSettings
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
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

# These cases assert DOM/identity behavior, not shared-runner navigation speed.
# Deadline arithmetic and budget enforcement have separate deterministic tests.
OFFLINE_NAVIGATION_SECONDS = 5


@pytest.mark.parametrize(
    ("html", "error"),
    [
        ('<input name="email">', AuthenticationRequiredError),
        ('<input name="approvals_code">', AccountActionRequiredError),
        ('<iframe src="about:blank#captcha"></iframe>', AccountActionRequiredError),
        ("<main>Content isn't available</main>", AccessDeniedError),
        ("<main>Temporarily blocked</main>", TransientNavigationError),
        ("<main>Unknown layout</main>", LayoutChangedError),
    ],
)
def test_mobile_dom_failure_states_stop_without_reading_posts(tmp_path, html, error):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(error):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_valid_post_quoting_error_text_is_still_a_post(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture(
                [
                    (
                        "101",
                        "Synthetic Author",
                        "Content isn't available. Temporarily blocked.",
                    )
                ]
            )
        )
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED


@pytest.fixture
def reader_contract(tmp_path: Path) -> Iterator[object]:
    group = parse_group_ref("contract-group")
    observed_at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def read(stories: list[tuple[str, str, str]]) -> tuple:
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
                        copied = copy_post_link(
                            page, menu, timeout_seconds=OFFLINE_NAVIGATION_SECONDS
                        )
                        href = resolve_post_link(
                            target,
                            copied,
                            payload,
                            frozenset({group.key}),
                            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
                        )
                        payloads.append({**payload, "href": href})
                return extract_posts(payloads, group, observed_at, limit=50)
            finally:
                target.close()
                context.unroute("**/*", route_mobile)

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


@pytest.mark.parametrize(
    ("missing", "component"),
    [
        ("scroller", "unique_mobile_scroller"),
        ("timestamp", "unique_primary_timestamp"),
        ("footer", "primary_action_footer"),
        ("hidden", "primary_mobile_card"),
        ("body", "resolved_primary_body_matches_source"),
        ("multiple", "unique_resolved_primary_post"),
    ],
)
def test_permalink_verification_deadline_logs_current_missing_layout(
    tmp_path, monkeypatch, missing, component
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            if missing == "scroller":
                page.evaluate(
                    "document.querySelector('[data-type=vscroller]').remove()"
                )
            elif missing == "timestamp":
                page.evaluate(
                    "document.querySelector('[data-story] [aria-label]')"
                    ".removeAttribute('aria-label')"
                )
            elif missing == "footer":
                page.evaluate(
                    "document.querySelector('[aria-label=\"Make a comment\"]').remove()"
                )
            elif missing == "multiple":
                page.evaluate(
                    "document.querySelector('[data-type=vscroller]').firstElementChild.remove();"
                    "const n=document.querySelector('[data-story]');"
                    "n.after(n.cloneNode(true))"
                )
            elif missing == "hidden":
                page.evaluate(
                    "document.querySelector('[data-story] button')"
                    ".style.visibility='hidden'"
                )
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Synthetic private author",
        "text": "Expected private body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture(
                    [("301", source["author"], "Different private body")]
                ),
            ),
        )
        with pytest.raises(LayoutChangedError, match=component):
            resolve_post_link(
                page,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )
    snapshots = [
        fields for message, fields in logged if message == "Mobile layout snapshot"
    ]
    assert len(snapshots) == 1
    assert snapshots[0]["phase"] == "post_verification"
    assert snapshots[0]["verification_post_count"] == (
        2 if missing == "multiple" else 1 if missing == "body" else 0
    )
    if missing in {"scroller", "timestamp", "footer", "hidden"}:
        assert component in snapshots[0]["missing_components"]
    if missing == "hidden":
        assert snapshots[0]["visible_primary_menus"] < snapshots[0]["primary_menus"]
    output = str(logged)
    assert "private" not in output and "facebook.com" not in output
    assert "October" not in output


@pytest.mark.parametrize(
    ("html", "error", "signal"),
    [
        ('<input name="email">', AuthenticationRequiredError, "has_login"),
        (
            '<input name="approvals_code">',
            AccountActionRequiredError,
            "has_account_action",
        ),
        (
            "<main>Content isn't available</main>",
            AccessDeniedError,
            "has_access_denied",
        ),
        (
            "<main>Temporarily blocked</main>",
            TransientNavigationError,
            "has_rate_limit",
        ),
    ],
)
def test_permalink_terminal_state_after_initial_render_is_not_a_layout_error(
    tmp_path, monkeypatch, html, error, signal
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            page.set_content(html + "Synthetic private content")
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Author",
        "text": "Expected body",
        "mediaCount": 0,
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture([("301", "Author", "Body")]),
            ),
        )
        with pytest.raises(error):
            resolve_post_link(
                page,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )
    snapshots = [
        fields for message, fields in logged if message == "Mobile layout snapshot"
    ]
    assert snapshots[0][signal] is True
    assert "Synthetic private content" not in str(logged)


def test_permalink_temporary_missing_footer_recovers_without_reload_or_warning(
    tmp_path, monkeypatch
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            page.evaluate("""() => {
                const footer = document.querySelector('[aria-label="Make a comment"]');
                footer.setAttribute('aria-label', 'Loading');
                setTimeout(() => {
                    footer.setAttribute('aria-label', 'Make a comment');
                }, 200);
            }""")
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Author",
        "text": "Body",
        "mediaCount": 0,
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
    }
    requests = []
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture([("301", "Author", "Body")]),
            )

        context.route("**/*", serve)
        assert resolve_post_link(
            page,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            source,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")
    assert len(requests) == 1
    assert logged == []


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
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("wrong", [None, "author", "body", "age", "identity"])
def test_mobile_relative_age_rollover_keeps_post_link_checks_strict(tmp_path, wrong):
    source_html = mobile_fixture([("301", "Author", "Body")]).replace(
        "Sunday, October 11, 2026 at 10:00 AM", "19 hours ago"
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(source_html)
        with mobile_records(page) as records:
            payload = records[0][1]
        assert isinstance(payload["timestampObservedAt"], int)
        target = context.new_page()
        author = "Wrong" if wrong == "author" else "Author"
        body = "Wrong" if wrong == "body" else "Body"
        age = "21 hours ago" if wrong == "age" else "20 hours ago"
        html = mobile_fixture([("301", author, body)]).replace(
            "Sunday, October 11, 2026 at 10:00 AM", age
        )
        if wrong == "identity":
            html += (
                "<script>history.replaceState(null,'',"
                "'/groups/contract-group/posts/999/')</script>"
            )
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )

        def resolve():
            return resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                payload,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if wrong is None:
            assert resolve().endswith("/posts/301/")
        else:
            with pytest.raises(LayoutChangedError):
                resolve()


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
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
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
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
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
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
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
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )


@pytest.mark.parametrize("changed_identity", [False, True, "before_render"])
def test_mobile_waits_for_permalink_body_to_finish_rendering(
    tmp_path, changed_identity
):
    source = {
        "author": "Author",
        "text": "Expected complete body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        html = mobile_fixture([("999", "Author", "Still rendering")])
        html += """<script>
          history.replaceState(null,'','/groups/contract-group/posts/999/');
          setTimeout(() => {
            const body = document.querySelector('[data-story="999"]')
              .children[2].querySelector('[data-mcomponent="ServerTextArea"]');
            body.textContent = 'Expected complete body';
          }, 250);
        </script>"""
        if changed_identity:
            html += """<script>setTimeout(() => history.replaceState(
              null, '', '/groups/contract-group/posts/888/'), 100);</script>"""
        if changed_identity == "before_render":
            html += """<script>
              const footer = document.querySelector('[aria-label="Make a comment"]');
              footer.hidden = true;
              setTimeout(() => footer.hidden = false, 250);
            </script>"""
        requests = []

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)

        def resolve():
            return resolve_post_link(
                page,
                "https://m.facebook.com/share/p/999/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if changed_identity:
            with pytest.raises(LayoutChangedError, match="unchanged_resolved_post"):
                resolve()
        else:
            assert resolve().endswith("/posts/999/")
        assert len(requests) == 1


@pytest.mark.parametrize(
    "destination",
    ["matching", "wrong_body", "wrong_identity", "account_action", "persistent_panel"],
)
def test_mobile_extra_panel_gets_one_strict_canonical_check(tmp_path, destination):
    source = {
        "author": "Author",
        "text": "Expected body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
        "bodyControlCount": 0,
        "partial": False,
    }
    panel = (
        '<div data-mcomponent="MContainer">'
        '<div data-mcomponent="ServerTextArea">Synthetic interface panel</div>'
        '<button role="button"><span data-mcomponent="TextArea">First</span>'
        '</button><button role="button"><span data-mcomponent="TextArea">'
        "Second</span></button></div>"
    )
    canonical = "https://m.facebook.com/groups/contract-group/posts/999/"
    requests = []
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def serve(route):
            requests.append(route.request.url)
            share = "/share/" in route.request.url
            body = "Expected body"
            if not share and destination == "wrong_body":
                body += " Different continuation"
            html = mobile_fixture([("999", "Author", body)])
            if share or destination == "persistent_panel":
                footer = (
                    '</div><div data-mcomponent="MContainer">'
                    '<button role="button" aria-label="Like,'
                )
                html = html.replace(footer, "</div>" + panel + footer[6:])
            path = "/groups/contract-group/posts/999/"
            if not share and destination == "wrong_identity":
                path = "/groups/contract-group/posts/888/"
            if not share and destination == "account_action":
                path = "/checkpoint/"
                html = '<input name="approvals_code">'
            html += f"<script>history.replaceState(null,'','{path}')</script>"
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        error = (
            AccountActionRequiredError
            if destination == "account_action"
            else LayoutChangedError
        )

        def resolve():
            return resolve_post_link(
                page,
                "https://m.facebook.com/share/p/999/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if destination == "matching":
            assert resolve() == canonical.replace("m.facebook.com", "www.facebook.com")
        else:
            with pytest.raises(error):
                resolve()
        assert requests == ["https://m.facebook.com/share/p/999/", canonical]


@pytest.mark.parametrize("footer_state", ["incomplete", "virtualized", "loading"])
def test_mobile_never_uses_a_reply_action_row_as_the_post_footer(
    tmp_path, footer_state
):
    html = mobile_fixture([("999", "Author", "Only the primary body")])
    primary_footer = (
        '</div><div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like, press and hold for more reactions">'
    )
    replacement = primary_footer.replace(
        '<div data-mcomponent="MContainer">',
        '<div data-mcomponent="MContainer" '
        'style="content-visibility:auto;contain-intrinsic-size:100px;'
        'margin-top:3000px">',
    )
    if footer_state == "virtualized":
        html = html.replace(primary_footer, replacement)
    else:
        html = html.replace('aria-label="Make a comment"', 'aria-label="Loading"')
    html = html.replace(
        "Following reply preview</div></div></div>",
        'Following reply preview</div><img alt="Reply avatar"></div>'
        '<div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like">Like</button>'
        '<button role="button" aria-label="Comment">Comment</button></div></div>',
    )
    if footer_state == "loading":
        html += """<script>setTimeout(() => document.querySelector(
          '[aria-label="Loading"]').setAttribute('aria-label','Make a comment'),
          250);</script>"""
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        if footer_state == "incomplete":
            with pytest.raises(LayoutChangedError, match="primary_action_footer"):
                wait_for_mobile_page(page, timeout_seconds=0.1)
        else:
            wait_for_mobile_page(page, timeout_seconds=1)
            with mobile_records(page) as records:
                assert len(records) == 1
                assert records[0][1]["text"] == "Only the primary body"
                assert records[0][1]["mediaCount"] == 0


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
    source = MobilePostSource(settings)
    with source._context(headless=True, timezone_id=timezone_id) as context:
        yield context


@pytest.mark.parametrize(
    "failure", ["feed", "post", "navigation", "unexpected", "auth", None]
)
def test_mobile_fetch_saves_only_failed_viewports_before_tabs_close(
    tmp_path, monkeypatch, failure
):
    from fbn import mobile

    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        opened = []
        context.on("page", lambda page: opened.append(page))

        def routes(route):
            html = mobile_fixture([("301", "Synthetic Author", "Synthetic body")])
            if "/share/" in route.request.url:
                html = (
                    mobile_fixture([("301", "Synthetic Author", "Different body")])
                    if failure == "post"
                    else html
                )
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            elif failure == "feed":
                html = "<main>Synthetic unsupported layout</main>"
            elif failure == "auth":
                html = '<input name="email" value="SYNTHETIC_SECRET">'
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        if failure in {"navigation", "unexpected"}:

            def fail(*args, **kwargs):
                if failure == "navigation":
                    raise mobile.PlaywrightTimeoutError("Synthetic timeout")
                raise ValueError("Synthetic unexpected failure")

            monkeypatch.setattr(mobile, "select_new_posts", fail)
        error = {
            "feed": LayoutChangedError,
            "post": LayoutChangedError,
            "navigation": TransientNavigationError,
            "unexpected": ValueError,
            "auth": AuthenticationRequiredError,
        }.get(failure)
        if error:
            with pytest.raises(error):
                source.fetch_recent(
                    parse_group_ref("contract-group"),
                    ScanPolicy(
                        sample_count=1, max_scrolls=0, navigation_timeout_seconds=5
                    ),
                )
        else:
            result = source.fetch_recent(
                parse_group_ref("contract-group"),
                ScanPolicy(sample_count=1, max_scrolls=0, navigation_timeout_seconds=5),
            )
            assert [post.post_id for post in result.posts] == ["301"]
        assert tuple(context.pages) == original_pages
        assert all(page.is_closed() for page in opened)
        root = tmp_path / "failure-screenshots"
        if failure in {None, "auth"}:
            assert not root.exists()
            return
        events = tuple(root.iterdir())
        assert len(events) == 1
        event = events[0]
        expected = (
            {"post": "saved", "feed": "saved"}
            if failure == "post"
            else {"feed": "saved"}
        )
        assert json.loads((event / "metadata.json").read_text())["pages"] == expected
        for role in expected:
            image = event / f"{role}.png"
            data = image.read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", data[16:24])
            assert 0 < width <= 2_000
            assert 0 < height <= 3_000
            assert stat.S_IMODE(image.stat().st_mode) == 0o600


def test_real_failure_screenshots_mask_input_and_editable_content(tmp_path):
    from fbn.failure_screenshots import capture_failure_screenshots

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<style>body{margin:0} input,textarea,div {box-sizing:border-box;"
            "width:200px;height:32px;display:block;border:0;padding:0;margin:0;"
            "overflow:hidden;}</style>"
            '<input value="Synthetic secret one">'
            "<textarea>Synthetic secret two</textarea>"
            '<div contenteditable="true">Synthetic secret three</div>'
        )
        capture_failure_screenshots(
            tmp_path / "unused", LayoutChangedError("Synthetic"), {"feed": page}
        )
        first = next((tmp_path / "failure-screenshots").iterdir()) / "feed.png"
        # Change values without mobile focus/zoom/scroll changing the viewport.
        page.evaluate("""() => {
            document.querySelector('input').value = 'Other secret one';
            document.querySelector('textarea').value = 'Other secret two';
            document.querySelector('[contenteditable]').textContent =
                'Other secret three';
        }""")
        capture_failure_screenshots(
            tmp_path / "unused", LayoutChangedError("Synthetic"), {"feed": page}
        )
        events = sorted((tmp_path / "failure-screenshots").iterdir())
        assert len(events) == 2
        assert first.read_bytes() == (events[-1] / "feed.png").read_bytes()


def test_bootstrap_layout_failure_saves_private_feed_before_closing(tmp_path):
    with _local_context(tmp_path) as context:
        opened = []
        context.on("page", lambda page: opened.append(page))
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<main>Synthetic unknown layout</main>",
            ),
        )
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        with pytest.raises(LayoutChangedError):
            source._validate_authenticated_profile(
                parse_group_ref("contract-group"), navigation_timeout_seconds=5
            )
        assert all(page.is_closed() for page in opened)
        event = next((tmp_path / "failure-screenshots").iterdir())
        assert (event / "feed.png").read_bytes().startswith(b"\x89PNG")


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


def test_persistent_profile_retains_facebook_cookie_across_headless_relaunch(
    tmp_path: Path,
) -> None:
    settings = BrowserSettings(
        browser=os.environ.get("FBN_TEST_BROWSER", "chromium"),
        profile_dir=tmp_path / "persistent-cookie-profile",
        headless=True,
    )
    source = MobilePostSource(settings)
    cookie = {
        "name": "fbn_persistence_probe",
        "value": "local-sanitized-value",
        "domain": ".facebook.com",
        "path": "/",
        "expires": 2_000_000_000,
        "secure": True,
    }
    with source._context(headless=True) as context:
        context.add_cookies([cookie])
    with source._context(headless=True) as context:
        persisted = context.cookies("https://www.facebook.com/")

    assert any(
        item["name"] == cookie["name"] and item["value"] == cookie["value"]
        for item in persisted
    )
