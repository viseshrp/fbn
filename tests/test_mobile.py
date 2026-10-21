from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fbn.exceptions import AccessDeniedError, ConfigurationError
from fbn.extractor import parse_facebook_timestamp, parse_group_ref, parse_post_url
from fbn.mobile import MobilePostSource, mobile_group_url, verified_group_keys
from fbn.models import Post
from tests.mobile_fixtures import post_view_fixture


@pytest.fixture(autouse=True)
def no_app_prompt_in_unit_page_doubles(monkeypatch):
    # Prompt ownership/actionability is tested with a real offline browser.
    monkeypatch.setattr("fbn.mobile._dismiss_app_prompt", lambda *args: False)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"postCount": 0, "missing": []}, ("primary_mobile_card",)),
        ({"postCount": 1, "missing": []}, ()),
        ({"postCount": 2, "missing": []}, ()),
        (
            {
                "postCount": 0,
                "missing": [
                    "primary_action_footer",
                    "unique_primary_timestamp",
                    "primary_action_footer",
                ],
            },
            ("primary_action_footer", "unique_primary_timestamp"),
        ),
    ],
)
def test_missing_mobile_components_name_absent_posts_without_page_content(
    result, expected
):
    from fbn.mobile import _missing_mobile_components

    assert _missing_mobile_components(result) == expected


@pytest.mark.parametrize(
    "field",
    [
        "feedRootCount",
        "itemCount",
        "postCount",
        "total_scrollers",
        "primary_menus",
        "dialogs",
        "visible_text_chars",
        "visible_controls",
        "visible_media",
    ],
)
@pytest.mark.parametrize("value", [1, None])
def test_loading_shell_requires_all_content_free_counters_to_be_zero(field, value):
    from fbn.mobile import _is_loading_shell

    result = dict.fromkeys(("feedRootCount", "itemCount", "postCount"), 0)
    structure = dict.fromkeys(
        (
            "total_scrollers",
            "primary_menus",
            "dialogs",
            "visible_text_chars",
            "visible_controls",
            "visible_media",
        ),
        0,
    )
    assert _is_loading_shell(result, structure)
    (result if field in result else structure)[field] = value
    assert not _is_loading_shell(result, structure)


@pytest.mark.parametrize(
    ("source_label", "target_label", "elapsed_ms", "matches"),
    [
        ("19 hours ago", "20 hours ago", 2000, True),
        ("19h", "20 hours ago", 2000, True),
        ("59 minutes ago", "1 hour ago", 2000, True),
        ("23 hours ago", "1 day ago", 2000, True),
        ("6 days ago", "1 week ago", 2000, True),
        ("just now", "1 minute ago", 2000, True),
        ("19 hours ago", "20 hours ago", 0, False),
        ("19 hours ago", "21 hours ago", 2000, False),
        ("19 hours ago", "18 hours ago", 2000, False),
        ("19 hours ago", "20 hours ago", -1, False),
        ("19 hours ago", "20 hours ago", 150001, False),
        ("19h", "19 hours ago", 0, True),
        ("26 Sep", "27 Sep", 2000, False),
        ("yesterday at 10:00", "today at 10:00", 2000, False),
    ],
)
def test_relative_publication_matches_only_overlapping_observed_age_ranges(
    source_label, target_label, elapsed_ms, matches
):
    from fbn.mobile import _publication_matches

    source = {
        "author": "Author",
        "timestampLabel": source_label,
        "timestampObservedAt": 1791878400000,
    }
    target = {
        **source,
        "timestampLabel": target_label,
        "timestampObservedAt": source["timestampObservedAt"] + elapsed_ms,
    }
    assert _publication_matches(target, source) is matches


@pytest.mark.parametrize("observed", [None, True, "123", float("nan"), float("inf")])
def test_relative_rollover_requires_valid_observation_times(observed):
    from fbn.mobile import _publication_matches

    source = {
        "author": "Author",
        "timestampLabel": "19 hours ago",
        "timestampObservedAt": observed,
    }
    target = {
        "author": "Author",
        "timestampLabel": "20 hours ago",
        "timestampObservedAt": 1791878402000,
    }
    assert not _publication_matches(target, source)
    assert not _publication_matches(source, target)


def test_relative_rollover_never_relaxes_author_or_body_checks():
    from fbn.mobile import _primary_post_comparisons

    source = {
        "author": "Author",
        "text": "Full body",
        "mediaCount": 0,
        "timestampLabel": "19 hours ago",
        "timestampObservedAt": 1791878400000,
    }
    target = {
        **source,
        "timestampLabel": "20 hours ago",
        "timestampObservedAt": 1791878402000,
    }
    assert all(_primary_post_comparisons(target, source).values())
    for field in ("author", "text"):
        assert not all(
            _primary_post_comparisons({**target, field: "Wrong"}, source).values()
        )


@pytest.mark.parametrize("partial_side", ["source", "target"])
def test_matching_preview_text_is_not_accepted_as_a_complete_verified_post(
    partial_side,
):
    from fbn.mobile import _primary_post_comparisons

    source = {
        "author": "Author",
        "text": "Same preview",
        "timestampLabel": "2h",
        "mediaCount": 0,
    }
    target = dict(source)
    (source if partial_side == "source" else target)["partial"] = True
    checks = _primary_post_comparisons(target, source)
    assert checks["resolved_primary_body_matches_source"]
    assert not checks["resolved_primary_body_complete"]


@pytest.mark.parametrize("action_refresh", [False, True])
def test_preview_expansion_shares_one_timeout_budget_and_disposes_handles(
    monkeypatch, action_refresh
):
    from fbn import mobile

    clock = [0.0]
    waits = []
    clicks = []
    disposed = []
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    source = {
        "author": "Author",
        "text": "Prefix.",
        "timestampLabel": "2h",
        "cardToken": 1,
        "actionToken": "menu-301",
        "partial": True,
        "previewControlCount": 1,
    }
    expanded = {
        **post_view_fixture("Prefix. Full body."),
        **source,
        "text": "Prefix. Full body.",
        "partial": False,
        "bodyState": "complete",
        "previewControlCount": 0,
        "cardToken": 2,
        "actionToken": "refreshed-action" if action_refresh else source["actionToken"],
    }

    def click(**kwargs):
        clicks.append(kwargs["timeout"])
        clock[0] += 3

    control = SimpleNamespace(evaluate=lambda *args: None, click=click)
    expansion = SimpleNamespace(
        as_element=lambda: control, dispose=lambda: disposed.append("expansion")
    )
    result = SimpleNamespace(
        json_value=lambda: expanded, dispose=lambda: disposed.append("result")
    )

    def wait(script, **kwargs):
        waits.append(kwargs["timeout"])
        if len(waits) == 1:
            clock[0] += 2
            return expansion
        return result

    menu = SimpleNamespace(evaluate=lambda *args: None)
    page = SimpleNamespace(
        url="https://m.facebook.com/groups/test/",
        bring_to_front=lambda: None,
        wait_for_function=wait,
    )
    assert (
        mobile.expand_post_preview(page, menu, source, timeout_seconds=10) is expanded
    )
    assert waits == [10_000, 5_000]
    assert clicks == [8_000]
    assert disposed == ["result", "expansion"]


def test_complete_mobile_post_does_not_trigger_expansion():
    from fbn.mobile import expand_post_preview

    payload = {"partial": False}
    assert expand_post_preview(None, None, payload, timeout_seconds=1) is payload


@pytest.mark.parametrize("fail", [False, True])
def test_link_navigation_observer_is_always_removed(monkeypatch, fail):
    from fbn import mobile

    events = []
    frame = SimpleNamespace(url="https://m.facebook.com/groups/test/posts/301/")
    page = SimpleNamespace(
        main_frame=frame,
        on=lambda event, callback: events.append((event, callback)),
        remove_listener=lambda event, callback: events.remove((event, callback)),
    )

    def resolve(target, copied, payload, groups, destinations, **kwargs):
        callback = events[0][1]
        callback(SimpleNamespace(url="https://m.facebook.com/groups/test/posts/999/"))
        for _ in range(20):
            callback(frame)
        assert [link.post_id for link in destinations] == ["301"]
        if fail:
            raise mobile.LayoutChangedError("Synthetic failure")
        return "Synthetic result"

    monkeypatch.setattr(mobile, "_resolve_post_link", resolve)
    if fail:
        with pytest.raises(mobile.LayoutChangedError):
            mobile.resolve_post_link(page, "unused", {}, frozenset(), timeout_seconds=1)
    else:
        assert (
            mobile.resolve_post_link(page, "unused", {}, frozenset(), timeout_seconds=1)
            == "Synthetic result"
        )
    assert events == []


@pytest.mark.parametrize("field", ["author", "text", "timestampLabel", "count"])
def test_primary_link_mismatch_identifies_failed_comparison_without_content(
    field, capsys
):
    from fbn.exceptions import LayoutChangedError
    from fbn.logging import configure_logging
    from fbn.mobile import _verify_primary_post

    source = {
        "author": "private-author",
        "text": "private-body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
        "partial": False,
    }
    target = dict(source)
    names = {
        "author": "resolved_primary_author_matches_source",
        "text": "resolved_primary_body_matches_source",
        "timestampLabel": "resolved_primary_publication_matches_source",
        "count": "unique_resolved_primary_post",
    }
    if field != "count":
        target[field] = "private-mismatch"
    source.update(bodyTextLengths=[12], bodyTextPartCount=1, bodyControlCount=0)
    target.update(
        bodyTextLengths=[12, 32, 10, 14], bodyTextPartCount=4, bodyControlCount=2
    )
    configure_logging(True)
    with pytest.raises(LayoutChangedError, match=names[field]):
        _verify_primary_post([] if field == "count" else [target], source)
    output = capsys.readouterr().out
    assert names[field] in output
    assert "private-" not in output
    assert "October" not in output
    if field != "count":
        assert "source text parts=[12]" in output
        assert "target text parts=[12, 32, 10, 14]" in output
        assert "target body controls=2" in output


def test_mobile_launch_failure_is_redacted(tmp_path):
    from playwright.sync_api import Error as PlaywrightError

    from fbn.config import BrowserSettings
    from fbn.exceptions import BrowserUnavailableError

    def fail(*args, **kwargs):
        raise PlaywrightError("private-driver-detail")

    runtime = SimpleNamespace(
        devices={"Pixel 7": {}},
        chromium=SimpleNamespace(launch_persistent_context=fail),
    )
    source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "profile"))
    with pytest.raises(BrowserUnavailableError) as caught:
        source._launch_context(
            runtime, profile_dir=tmp_path / "profile", headless=True, timezone_id=None
        )
    assert "private-driver-detail" not in str(caught.value)


def test_mobile_urls_normalize_to_existing_desktop_identity():
    group = parse_group_ref("https://m.facebook.com/groups/test-group/")
    assert group.url == "https://www.facebook.com/groups/test-group/"
    assert mobile_group_url(group).startswith(
        "https://m.facebook.com/groups/test-group/"
    )
    mobile = parse_post_url("https://m.facebook.com/groups/test-group/posts/123/")
    desktop = parse_post_url("https://www.facebook.com/groups/test-group/posts/123/")
    assert mobile == desktop


@pytest.mark.parametrize(
    "url",
    [
        "https://m.facebook.com.evil.invalid/groups/test-group/",
        "http://m.facebook.com/groups/test-group/",
        "https://user@m.facebook.com/groups/test-group/",
        "https://m.facebook.com:443/groups/test-group/",
    ],
)
def test_mobile_does_not_weaken_group_url_allowlist(url):
    with pytest.raises(ConfigurationError):
        parse_group_ref(url)


def redirect_response(*urls):
    request = None
    for url in urls:
        request = SimpleNamespace(url=url, redirected_from=request)
    return SimpleNamespace(request=request)


def test_numeric_group_alias_requires_a_trusted_http_redirect_chain():
    group = parse_group_ref("123")
    final = "https://m.facebook.com/groups/test-group/"
    response = redirect_response(mobile_group_url(group), final)
    assert verified_group_keys(response, group, final) == {"123", "test-group"}


@pytest.mark.parametrize(
    "urls",
    [
        ("https://m.facebook.com/groups/test-group/",),
        (
            "https://evil.invalid/groups/123/",
            "https://m.facebook.com/groups/test-group/",
        ),
        (
            "https://m.facebook.com/groups/123/",
            "https://m.facebook.com/groups/unrelated/",
            "https://m.facebook.com/groups/test-group/",
        ),
    ],
)
def test_unproven_or_cross_group_aliases_are_rejected(urls):
    with pytest.raises(AccessDeniedError):
        verified_group_keys(redirect_response(*urls), parse_group_ref("123"), urls[-1])


def history_proof(*events):
    from fbn.mobile import InitialGroupNavigation

    proof = InitialGroupNavigation("main")
    for kind, url in events:
        if kind == "document":
            proof.document_event({"frame": {"id": "main", "url": url}})
        else:
            proof.history_event({"frameId": "main", "url": url, "navigationType": kind})
    return proof


def test_initial_same_document_group_rename_is_verified():
    group = parse_group_ref("123")
    initial = mobile_group_url(group)
    final = "https://m.facebook.com/groups/test-group/"
    response = redirect_response(initial)
    response.status, response.url = 200, initial
    proof = history_proof(("document", initial), ("historyApi", final))
    assert verified_group_keys(response, group, final, navigation=proof) == {
        "123",
        "test-group",
    }


@pytest.mark.parametrize(
    "events",
    [
        [("historyApi", "https://m.facebook.com/groups/test-group/")],
        [("document", "https://m.facebook.com/groups/unrelated/")],
        [("document", "https://evil.invalid/groups/123/")],
        [("document", "https://m.facebook.com/groups/test-group/")],
        [
            ("document", "https://m.facebook.com/groups/123/"),
            ("document", "https://m.facebook.com/groups/test-group/"),
        ],
        [
            ("document", "https://m.facebook.com/groups/123/"),
            ("historyApi", "https://m.facebook.com/groups/unrelated/"),
            ("historyApi", "https://m.facebook.com/groups/test-group/"),
        ],
        [
            ("document", "https://m.facebook.com/groups/123/"),
            ("fragment", "https://m.facebook.com/groups/test-group/"),
        ],
    ],
)
def test_history_alias_rejects_unbound_or_cross_document_evidence(events):
    initial = "https://m.facebook.com/groups/123/"
    response = redirect_response(initial)
    response.status, response.url = 200, initial
    with pytest.raises(AccessDeniedError):
        verified_group_keys(
            response,
            parse_group_ref("123"),
            "https://m.facebook.com/groups/test-group/",
            navigation=history_proof(*events),
        )


@pytest.mark.parametrize("status", [403, 429, 500])
def test_history_alias_requires_successful_group_response(status):
    initial = mobile_group_url(parse_group_ref("123"))
    final = "https://m.facebook.com/groups/test-group/"
    response = redirect_response(initial)
    response.status, response.url = status, initial
    with pytest.raises(AccessDeniedError):
        verified_group_keys(
            response,
            parse_group_ref("123"),
            final,
            navigation=history_proof(("document", initial), ("historyApi", final)),
        )


def test_subframe_events_cannot_prove_group_rename():
    proof = history_proof(("document", "https://m.facebook.com/groups/123/"))
    proof.history_event(
        {
            "frameId": "child",
            "navigationType": "historyApi",
            "url": "https://m.facebook.com/groups/test-group/",
        }
    )
    response = redirect_response("https://m.facebook.com/groups/123/")
    response.status, response.url = 200, response.request.url
    with pytest.raises(AccessDeniedError):
        verified_group_keys(
            response,
            parse_group_ref("123"),
            "https://m.facebook.com/groups/test-group/",
            navigation=proof,
        )


@pytest.mark.parametrize("invalid", ["overflow", "response", "final", "numeric"])
def test_history_alias_is_bounded_and_bound_to_response_and_final_url(invalid):
    initial = mobile_group_url(parse_group_ref("123"))
    final = "https://m.facebook.com/groups/test-group/"
    response = redirect_response(initial)
    response.status, response.url = 200, initial
    proof = history_proof(("document", initial), ("historyApi", final))
    if invalid == "overflow":
        for _ in range(10):
            proof.history_event(
                {"frameId": "main", "url": final, "navigationType": "historyApi"}
            )
    elif invalid == "response":
        response.url = "https://m.facebook.com/groups/999/"
    elif invalid == "final":
        final = "https://m.facebook.com/groups/another/"
    else:
        final = "https://m.facebook.com/groups/999/"
        proof = history_proof(("document", initial), ("historyApi", final))
    with pytest.raises(AccessDeniedError):
        verified_group_keys(response, parse_group_ref("123"), final, navigation=proof)


def test_mobile_accessible_timestamp_is_parsed_in_configured_timezone():
    parsed = parse_facebook_timestamp(
        "Sunday, October 11, 2026 at 10:00 AM",
        datetime(2026, 10, 11, 16, tzinfo=timezone.utc),
        timezone_name="America/New_York",
    )
    assert parsed is not None
    assert parsed.astimezone(timezone.utc).hour == 14


def test_sort_activates_feed_tab_before_reading_controls(monkeypatch):
    from fbn import mobile

    activated = []
    page = SimpleNamespace(bring_to_front=lambda: activated.append(True))

    def control(*args, **kwargs):
        assert activated == [True]
        return SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr(mobile, "_control", control)
    mobile.select_new_posts(page, timeout_seconds=1)


def test_sort_actions_share_one_timeout_budget(monkeypatch):
    from fbn import mobile

    clock = [0.0]
    waits = []
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])

    def button(seconds):
        def click(**kwargs):
            clock[0] += seconds

        return SimpleNamespace(click=click, dispose=lambda: None)

    sort, choice = button(2), button(3)
    monkeypatch.setattr(
        mobile,
        "_control",
        lambda page, label, **kwargs: (
            None if kwargs.get("feed_only") else sort if label == "Sort" else choice
        ),
    )
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        wait_for_function=lambda script, **kwargs: waits.append(kwargs["timeout"]),
    )
    mobile.select_new_posts(page, timeout_seconds=10)
    assert waits == [5_000]


def test_link_navigation_and_render_share_one_timeout_budget(monkeypatch):
    from contextlib import contextmanager

    from fbn import mobile

    clock = [0.0]
    waits = []
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    payload = {"author": "Author", "text": "Body", "timestampLabel": "2 hours ago"}
    canonical = "https://m.facebook.com/groups/test-group/posts/301/"

    def navigate(*args, **kwargs):
        clock[0] += 8
        return SimpleNamespace(status=200)

    @contextmanager
    def records(*args, **kwargs):
        yield [(None, payload)]

    monkeypatch.setattr(mobile, "mobile_records", records)
    monkeypatch.setattr(
        mobile, "wait_for_mobile_page", lambda page, **kw: waits.append(kw)
    )
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        goto=navigate,
        url=canonical,
        on=lambda *args: None,
        remove_listener=lambda *args: None,
    )
    assert mobile.resolve_post_link(
        page,
        "https://m.facebook.com/share/p/example/",
        payload,
        frozenset({"test-group"}),
        timeout_seconds=30,
    ) == canonical.replace("m.facebook.com", "www.facebook.com")
    assert waits[0]["timeout_seconds"] == 22


@pytest.mark.parametrize("navigation_seconds,expected_budget", [(8, 15), (26, 2)])
def test_resolved_preview_expansion_uses_original_link_budget_and_live_menu(
    monkeypatch, navigation_seconds, expected_budget
):
    from contextlib import contextmanager

    from fbn import mobile

    clock = [0.0]
    active = [False]
    expansion_calls = []
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    source = {
        "author": "Author",
        "text": "Prefix. Full body.",
        "timestampLabel": "2 hours ago",
        "mediaCount": 0,
        "partial": False,
    }
    canonical = "https://m.facebook.com/groups/test-group/posts/301/"
    menu = object()

    def navigate(*args, **kwargs):
        clock[0] += navigation_seconds
        return SimpleNamespace(status=200)

    def wait(*args, **kwargs):
        clock[0] += 2

    @contextmanager
    def records(*args, **kwargs):
        active[0] = True
        try:
            yield [(menu, {**source, "text": "Prefix.", "partial": True})]
        finally:
            active[0] = False

    def expand(page, handle, payload, **kwargs):
        assert active[0] and handle is menu
        expansion_calls.append(kwargs)
        return source

    monkeypatch.setattr(mobile, "mobile_records", records)
    monkeypatch.setattr(mobile, "wait_for_mobile_page", wait)
    monkeypatch.setattr(mobile, "expand_post_preview", expand)
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        goto=navigate,
        url=canonical,
        on=lambda *args: None,
        remove_listener=lambda *args: None,
    )
    assert mobile.resolve_post_link(
        page, canonical, source, frozenset({"test-group"}), timeout_seconds=30
    ).endswith("/posts/301/")
    assert expansion_calls == [{"timeout_seconds": expected_budget, "permalink": True}]
    assert not active[0]


def test_expired_mobile_budget_fails_without_starting_another_action(monkeypatch):
    from fbn import mobile
    from fbn.exceptions import TransientNavigationError

    monkeypatch.setattr(mobile.time, "monotonic", lambda: 31)
    with pytest.raises(TransientNavigationError, match="phase=post_render"):
        mobile._remaining_seconds(30, "post_render")


def test_browser_action_failure_logs_phase_without_exception_content(monkeypatch):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        SimpleNamespace(warning=lambda message, **kw: logged.append((message, kw))),
    )
    with (
        pytest.raises(mobile.PlaywrightError),
        mobile._action_phase("post_navigation"),
    ):
        raise mobile.PlaywrightError("Synthetic private URL and account content")
    assert logged[0][1]["phase"] == "post_navigation"
    assert "Synthetic private" not in str(logged)


def test_scan_restores_feed_focus_and_uses_configured_render_timeout(monkeypatch):
    from contextlib import contextmanager

    from fbn import mobile
    from fbn.models import ScanPolicy

    events = []
    page = SimpleNamespace(
        bring_to_front=lambda: events.append("front"),
        url="https://m.facebook.com/groups/test-group/",
    )

    def wait(page, **kwargs):
        assert events[-1] == "front"
        events.append(kwargs["timeout_seconds"])

    @contextmanager
    def records(*args, **kwargs):
        yield []

    monkeypatch.setattr(mobile, "wait_for_mobile_page", wait)
    monkeypatch.setattr(mobile, "mobile_records", records)
    source = MobilePostSource(SimpleNamespace())
    source._scan_mobile(
        page,
        None,
        parse_group_ref("test-group"),
        ScanPolicy(max_scrolls=0, navigation_timeout_seconds=30),
        frozenset({"test-group"}),
        None,
    )
    assert events == ["front", 30]


@pytest.mark.parametrize("failure", ["route", "validation", None])
def test_post_target_always_closes_its_tab_and_guards_navigation(failure):
    from fbn import mobile

    events = []

    def install_route(pattern, guard):
        assert pattern == "**/*"
        guard(
            SimpleNamespace(
                request=SimpleNamespace(
                    url="https://evil.invalid/", is_navigation_request=lambda: True
                ),
                abort=lambda: events.append("abort"),
                fallback=lambda: events.append("fallback"),
            )
        )
        if failure == "route":
            raise mobile.PlaywrightError("Synthetic failure")

    page = SimpleNamespace(route=install_route, close=lambda: events.append("close"))
    context = SimpleNamespace(new_page=lambda: page)

    def run():
        with mobile._post_target(context) as target:
            assert target is page
            if failure == "validation":
                raise mobile.LayoutChangedError("Synthetic mismatch")

    if failure:
        with pytest.raises((mobile.PlaywrightError, mobile.LayoutChangedError)):
            run()
    else:
        run()
    assert events == ["abort", "close"]


@pytest.mark.parametrize("capture_fails", [False, True])
def test_post_target_captures_before_close_and_preserves_original_error(capture_fails):
    from fbn import mobile

    events = []
    page = SimpleNamespace(
        route=lambda *args: None, close=lambda: events.append("close")
    )
    error = mobile.LayoutChangedError("Synthetic mismatch")

    def capture(target, failure):
        assert target is page
        assert failure is error
        assert events == []
        events.append("capture")
        if capture_fails:
            raise OSError("Synthetic full disk")

    with (
        pytest.raises(mobile.LayoutChangedError) as caught,
        mobile._post_target(SimpleNamespace(new_page=lambda: page), on_error=capture),
    ):
        raise error
    assert caught.value is error
    assert events == ["capture", "close"]


def test_copy_actions_share_budget_but_cleanup_has_its_own_budget(monkeypatch):
    from fbn import mobile

    clock = [0.0]
    actions = []
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])

    def action(name, seconds, **kwargs):
        actions.append((name, kwargs["timeout"]))
        clock[0] += seconds

    copy = SimpleNamespace(
        count=lambda: 0,
        wait_for=lambda **kw: action(
            kw["state"], 6 if kw["state"] == "visible" else 1, **kw
        ),
        click=lambda **kw: action("copy", 2, **kw),
    )
    close = SimpleNamespace(click=lambda **kw: action("close", 1, **kw))
    restored = []

    def evaluate(script):
        if script == mobile._CAPTURE_RESTORE_SCRIPT:
            restored.append(True)
        if script == "window.__fbnCopyCapture.values":
            return ["https://m.facebook.com/share/p/example/"]

    page = SimpleNamespace(
        bring_to_front=lambda: None,
        get_by_role=lambda role, **kw: copy if kw["name"] == "Copy link" else close,
        evaluate=evaluate,
        wait_for_function=lambda script, **kw: action("clipboard", 3, **kw),
    )
    menu = SimpleNamespace(click=lambda **kw: action("open", 8, **kw))
    mobile.copy_post_link(page, menu, timeout_seconds=30)
    assert actions == [
        ("open", 30_000),
        ("visible", 22_000),
        ("copy", 16_000),
        ("clipboard", 14_000),
        ("close", 5_000),
        ("hidden", 4_000),
    ]
    assert restored == [True]


@pytest.mark.parametrize("author_prefix", ["", "Author, "])
def test_publication_label_removes_only_verified_author_and_privacy(author_prefix):
    from fbn.mobile import _publication_label

    label = _publication_label(
        {
            "author": "Author",
            "timestampLabel": f", {author_prefix}2 hours ago, Private group",
        }
    )
    assert label == "2 hours ago"
    assert (
        _publication_label(
            {"author": "Author", "timestampLabel": "Another, 2 hours ago, Public group"}
        )
        != label
    )


def test_newer_post_below_marker_is_ordered_before_marker():
    now = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)

    def post(key, age, position):
        return Post(
            "test-group",
            key,
            f"https://www.facebook.com/groups/test-group/posts/{key}/",
            key,
            "Author",
            now,
            position,
            published_at=now - timedelta(minutes=age),
        )

    result = MobilePostSource._scan_result(
        [post("302", 5, 0), post("300", 120, 1), post("301", 60, 2)],
        1,
        False,
        "300",
    )
    assert [item.post_id for item in result.posts] == ["302", "301", "300"]
    assert [item.position for item in result.posts] == [0, 1, 2]


def test_unknown_publication_time_keeps_catch_up_incomplete():
    now = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
    post = Post(
        "test-group",
        "300",
        "https://www.facebook.com/groups/test-group/posts/300/",
        "Body",
        "Author",
        now,
        0,
    )
    result = MobilePostSource._scan_result([post], 1, False, "300")
    assert result.bounded


def test_same_age_post_below_marker_is_included_conservatively():
    now = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
    posts = [
        Post(
            "test-group",
            key,
            f"https://www.facebook.com/groups/test-group/posts/{key}/",
            "Body",
            "Author",
            now,
            position,
            published_at=now - timedelta(hours=2),
        )
        for position, key in enumerate(("300", "301"))
    ]
    result = MobilePostSource._scan_result(posts, 1, False, "300")
    assert [post.post_id for post in result.posts] == ["301", "300"]
