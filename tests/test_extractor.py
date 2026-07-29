from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fbn.exceptions import ConfigurationError
from fbn.extractor import (
    chronological_group_url,
    extract_posts,
    normalize_visible_text,
    parse_facebook_timestamp,
    parse_group_ref,
    parse_post_url,
)

OBSERVED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["123456", "local-group", "local.group_2"])
def test_parse_group_ref_accepts_simple_keys(value: str) -> None:
    group = parse_group_ref(value)

    assert group.key == value
    assert group.url == f"https://www.facebook.com/groups/{value}/"


def test_parse_group_ref_canonicalizes_facebook_url() -> None:
    group = parse_group_ref(
        "https://facebook.com/groups/local.group/?sorting_setting=CHRONOLOGICAL#feed"
    )

    assert group.key == "local.group"
    assert group.url == "https://www.facebook.com/groups/local.group/"
    assert chronological_group_url(group) == (
        "https://www.facebook.com/groups/local.group/?sorting_setting=CHRONOLOGICAL"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "group with spaces",
        "-leading-punctuation",
        "http://www.facebook.com/groups/local/",
        "https://evil.example/groups/local/",
        "https://facebook.com.evil.example/groups/local/",
        "https://user@facebook.com/groups/local/",
        "https://www.facebook.com:443/groups/local/",
        "https://www.facebook.com/groups/local/posts/123/",
        "/groups/local/",
    ],
)
def test_parse_group_ref_rejects_hostile_or_malformed_values(value: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_group_ref(value)


def test_parse_post_url_accepts_posts_and_permalink_and_strips_tracking() -> None:
    post = parse_post_url(
        "https://facebook.com/groups/local/posts/123/?__cft__[0]=tracking#comment"
    )
    permalink = parse_post_url(
        "https://www.facebook.com/groups/local/permalink/456?ref=share"
    )
    opaque = parse_post_url(
        "https://www.facebook.com/groups/local/posts/pfbid02AbCdEf123/"
    )

    assert post is not None
    assert (post.group_key, post.post_id, post.url) == (
        "local",
        "123",
        "https://www.facebook.com/groups/local/posts/123/",
    )
    assert permalink is not None
    assert (permalink.group_key, permalink.post_id, permalink.url) == (
        "local",
        "456",
        "https://www.facebook.com/groups/local/permalink/456/",
    )
    assert opaque is not None
    assert (opaque.post_id, opaque.url) == (
        "pfbid02AbCdEf123",
        "https://www.facebook.com/groups/local/posts/pfbid02AbCdEf123/",
    )


def test_parse_post_url_accepts_group_photo_identity_link() -> None:
    photo = parse_post_url(
        "https://www.facebook.com/photo/?fbid=987"
        "&set=gm.27558491783808657"
        "&idorvanity=1663189947098862"
        "&__cft__[0]=tracking"
    )

    assert photo is not None
    assert (photo.group_key, photo.post_id, photo.url) == (
        "1663189947098862",
        "27558491783808657",
        "https://www.facebook.com/groups/1663189947098862/posts/27558491783808657/",
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "javascript:alert(1)",
        "http://www.facebook.com/groups/local/posts/123/",
        "https://evil.example/groups/local/posts/123/",
        "https://facebook.com.evil.example/groups/local/posts/123/",
        "https://user@facebook.com/groups/local/posts/123/",
        "https://www.facebook.com:443/groups/local/posts/123/",
        "https://www.facebook.com/groups/local/posts/not-numeric/",
        "https://www.facebook.com/groups/local/posts/0/",
        "https://www.facebook.com/groups/local/photos/123/",
        "https://www.facebook.com/groups/local/posts/123/comments/",
        "https://www.facebook.com/photo/?fbid=987&set=gm.123",
        "https://www.facebook.com/photo/?fbid=987&idorvanity=local",
        "https://www.facebook.com/photo/?set=gm.123&idorvanity=local&idorvanity=other",
    ],
)
def test_parse_post_url_rejects_noncanonical_or_hostile_links(
    value: object,
) -> None:
    assert parse_post_url(value) is None


def test_extract_posts_normalizes_deduplicates_and_handles_missing_fields() -> None:
    group = parse_group_ref("local")
    payloads = [
        {
            "href": "https://www.facebook.com/groups/local/posts/300/?ref=share",
            "text": "  First\u00a0 post \n text ",
            "author": "  Ada\tLovelace ",
            "position": 2,
            "partial": True,
        },
        {
            "href": "https://www.facebook.com/groups/local/permalink/100/",
            "position": 0,
        },
        {
            "href": "https://www.facebook.com/groups/local/posts/300/",
            "text": "duplicate must not replace first",
            "author": "Different author",
            "position": 1,
        },
        {
            "href": "https://www.facebook.com/groups/another/posts/200/",
            "text": "wrong group",
            "position": 1,
        },
        {"text": "missing href", "position": 1},
        {
            "href": "https://evil.example/groups/local/posts/999/",
            "text": "hostile",
            "position": 1,
        },
    ]

    posts = extract_posts(payloads, group, OBSERVED_AT, limit=10)

    assert [post.post_id for post in posts] == ["100", "300"]
    assert posts[0].text == ""
    assert posts[0].author is None
    assert posts[0].position == 0
    assert posts[1].text == "First post text"
    assert posts[1].author == "Ada Lovelace"
    assert posts[1].partial is True
    assert posts[1].position == 2
    assert posts[1].observed_at is OBSERVED_AT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Just now", OBSERVED_AT),
        ("41m", OBSERVED_AT - timedelta(minutes=41)),
        ("22h", OBSERVED_AT - timedelta(hours=22)),
        ("2 minutes ago", OBSERVED_AT - timedelta(minutes=2)),
        ("Today at 09:30", datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc)),
        (
            "Yesterday at 14:48",
            datetime(2026, 7, 27, 14, 48, tzinfo=timezone.utc),
        ),
        (
            "26 July at 14:48",
            datetime(2026, 7, 26, 14, 48, tzinfo=timezone.utc),
        ),
    ],
)
def test_parse_facebook_timestamp_accepts_rendered_formats(
    value: str,
    expected: datetime,
) -> None:
    assert parse_facebook_timestamp(value, OBSERVED_AT) == expected


def test_parse_facebook_timestamp_uses_configured_calendar_timezone() -> None:
    observed_at = datetime(2026, 7, 30, 2, 30, tzinfo=timezone.utc)
    new_york = ZoneInfo("America/New_York")

    parsed = parse_facebook_timestamp(
        "Today at 21:00",
        observed_at,
        timezone_name="America/New_York",
    )

    assert parsed == datetime(2026, 7, 29, 21, 0, tzinfo=new_york)
    assert parsed is not None
    assert parsed.tzinfo is new_york


@pytest.mark.parametrize(
    "value",
    [None, "", "Sponsored", "characters in DOM order", "Yesterday at 25:00"],
)
def test_parse_facebook_timestamp_rejects_unknown_values(value: object) -> None:
    assert parse_facebook_timestamp(value, OBSERVED_AT) is None


def test_parse_facebook_timestamp_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        parse_facebook_timestamp(
            "41m",
            OBSERVED_AT,
            timezone_name="Mars/Olympus_Mons",
        )


def test_extract_posts_has_deterministic_position_order_and_post_limit() -> None:
    group = parse_group_ref("local")
    payloads = [
        {
            "href": "https://www.facebook.com/groups/local/posts/300/",
            "position": 2,
        },
        {
            "href": "https://www.facebook.com/groups/local/posts/100/",
            "position": 0,
        },
        {
            "href": "https://www.facebook.com/groups/local/posts/200/",
            "position": 1,
        },
    ]

    posts = extract_posts(payloads, group, OBSERVED_AT, limit=2)

    assert [post.post_id for post in posts] == ["100", "200"]
    assert len(posts) == 2


def test_normalize_visible_text_normalizes_unicode_whitespace_and_caps() -> None:
    value = "  \uff28\uff45\uff4c\uff4c\uff4f\tCafe\u0301 \n world  "

    assert normalize_visible_text(value, limit=12) == "Hello Café w"
    assert normalize_visible_text(None, limit=12) == ""


def test_extract_posts_caps_normalized_text() -> None:
    posts = extract_posts(
        [
            {
                "href": "https://www.facebook.com/groups/local/posts/123/",
                "text": "  ab\tcd ef  ",
            }
        ],
        parse_group_ref("local"),
        OBSERVED_AT,
        limit=1,
        text_limit=5,
    )

    assert posts[0].text == "ab cd"


@pytest.mark.parametrize(
    ("limit", "text_limit"),
    [(0, 4_000), (-1, 4_000), (True, 4_000), (1, 0), (1, False)],
)
def test_extract_posts_requires_positive_limits(
    limit: int,
    text_limit: int,
) -> None:
    with pytest.raises(ValueError):
        extract_posts(
            [],
            parse_group_ref("local"),
            OBSERVED_AT,
            limit=limit,
            text_limit=text_limit,
        )
