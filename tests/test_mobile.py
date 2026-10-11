from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fbn.exceptions import AccessDeniedError, ConfigurationError
from fbn.extractor import parse_facebook_timestamp, parse_group_ref, parse_post_url
from fbn.mobile import MobilePostSource, mobile_group_url, verified_group_keys
from fbn.models import Post


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


def test_mobile_accessible_timestamp_is_parsed_in_configured_timezone():
    parsed = parse_facebook_timestamp(
        "Sunday, October 11, 2026 at 10:00 AM",
        datetime(2026, 10, 11, 16, tzinfo=timezone.utc),
        timezone_name="America/New_York",
    )
    assert parsed is not None
    assert parsed.astimezone(timezone.utc).hour == 14


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
