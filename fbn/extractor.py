"""Validate Facebook URLs and normalize visible post payloads."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser

from .exceptions import ConfigurationError
from .models import GroupRef, Post

FACEBOOK_ORIGIN = "https://www.facebook.com"
FACEBOOK_HOSTS = frozenset({"facebook.com", "www.facebook.com"})
DEFAULT_TEXT_LIMIT = 4_000
MAX_AUTHOR_LENGTH = 256

_GROUP_KEY_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_POST_ID_PATTERN = r"[A-Za-z0-9]{1,200}"
_SIMPLE_GROUP_RE = re.compile(rf"^{_GROUP_KEY_PATTERN}$")
_GROUP_PATH_RE = re.compile(rf"^/groups/(?P<group>{_GROUP_KEY_PATTERN})/?$")
_POST_PATH_RE = re.compile(
    rf"^/groups/(?P<group>{_GROUP_KEY_PATTERN})/"
    rf"(?P<kind>posts|permalink)/(?P<post>{_POST_ID_PATTERN})/?$"
)
_PHOTO_SET_RE = re.compile(rf"^gm\.(?P<post>{_POST_ID_PATTERN})$")
_WHITESPACE_RE = re.compile(r"\s+")
_CLOCK_PATTERN = r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9]"
_FACEBOOK_TIMESTAMP_RE = re.compile(
    r"^(?:"
    r"just now"
    r"|[0-9]+\s*[smhdw]"
    r"|[0-9]+\s+(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago"
    rf"|(?:today|yesterday)\s+at\s+{_CLOCK_PATTERN}"
    rf"|[0-9]{{1,2}}\s+[A-Za-z]+\s+at\s+{_CLOCK_PATTERN}"
    r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PostLink:
    """A validated Facebook group post link."""

    group_key: str
    post_id: str
    url: str


def _facebook_path(value: str) -> str | None:
    """Return the path for a strict Facebook HTTPS URL."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in FACEBOOK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    return parsed.path


def _canonical_group_url(group_key: str) -> str:
    return f"{FACEBOOK_ORIGIN}/groups/{group_key}/"


def parse_group_ref(value: str) -> GroupRef:
    """Parse a simple group key or an HTTPS Facebook group URL.

    The strict host and path allowlist prevents a caller-controlled group value
    from turning the browser adapter into an arbitrary URL fetcher.
    """

    if not isinstance(value, str):
        raise ConfigurationError("The Facebook group must be a string.")

    candidate = value.strip()
    if _SIMPLE_GROUP_RE.fullmatch(candidate):
        group_key = candidate
    else:
        path = _facebook_path(candidate)
        match = _GROUP_PATH_RE.fullmatch(path) if path is not None else None
        if match is None:
            raise ConfigurationError(
                "Use a simple Facebook group ID/slug or an HTTPS facebook.com "
                "group URL."
            )
        group_key = match.group("group")

    return GroupRef(key=group_key, url=_canonical_group_url(group_key))


def chronological_group_url(group: GroupRef) -> str:
    """Return the canonical group feed URL requesting chronological sorting."""

    if not _SIMPLE_GROUP_RE.fullmatch(group.key):
        raise ConfigurationError("The Facebook group ID/slug is malformed.")
    return f"{_canonical_group_url(group.key)}?sorting_setting=CHRONOLOGICAL"


def parse_post_url(value: object) -> PostLink | None:
    """Parse a supported Facebook group post identity URL, if valid."""

    if not isinstance(value, str):
        return None

    path = _facebook_path(value)
    match = _POST_PATH_RE.fullmatch(path) if path is not None else None
    if match is not None:
        group_key = match.group("group")
        post_id = match.group("post")
        kind = match.group("kind")
        canonical_url = f"{FACEBOOK_ORIGIN}/groups/{group_key}/{kind}/{post_id}/"
    elif path in {"/photo", "/photo/"}:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        set_values = query.get("set", ())
        group_values = query.get("idorvanity", ())
        photo_match = (
            _PHOTO_SET_RE.fullmatch(set_values[0]) if len(set_values) == 1 else None
        )
        if (
            photo_match is None
            or len(group_values) != 1
            or _SIMPLE_GROUP_RE.fullmatch(group_values[0]) is None
        ):
            return None
        group_key = group_values[0]
        post_id = photo_match.group("post")
        canonical_url = f"{FACEBOOK_ORIGIN}/groups/{group_key}/posts/{post_id}/"
    else:
        return None

    if post_id.isdigit() and post_id.startswith("0"):
        return None
    return PostLink(
        group_key=group_key,
        post_id=post_id,
        url=canonical_url,
    )


def normalize_visible_text(value: object, *, limit: int) -> str:
    """Normalize Unicode and whitespace, then enforce a character limit."""

    _require_positive_int(limit, name="text limit")
    if not isinstance(value, str):
        return ""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized[:limit].rstrip()


def parse_facebook_timestamp(
    value: object,
    observed_at: datetime,
    *,
    timezone_name: str = "UTC",
) -> datetime | None:
    """Parse one rendered English Facebook timestamp in an IANA timezone."""

    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("observed_at must be a timezone-aware datetime")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone_name must be an IANA timezone name") from exc
    if not isinstance(value, str):
        return None

    text = normalize_visible_text(value, limit=128)
    if not text or _FACEBOOK_TIMESTAMP_RE.fullmatch(text) is None:
        return None

    relative_base = observed_at.astimezone(local_timezone)
    try:
        parsed = dateparser.parse(
            text,
            languages=["en"],
            settings={
                "DATE_ORDER": "DMY",
                "PREFER_DATES_FROM": "past",
                "RELATIVE_BASE": relative_base,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": timezone_name,
                "TO_TIMEZONE": timezone_name,
            },
        )
    except (OverflowError, TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(local_timezone)


def _require_positive_int(value: object, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def _payload_position(payload: Mapping[str, Any], fallback: int) -> int:
    value = payload.get("position")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def extract_posts(
    payloads: Iterable[Mapping[str, Any]],
    group: GroupRef,
    observed_at: datetime,
    limit: int,
    *,
    text_limit: int = DEFAULT_TEXT_LIMIT,
    allowed_group_keys: Collection[str] | None = None,
    timezone_name: str = "UTC",
) -> tuple[Post, ...]:
    """Convert ordered DOM payloads into bounded, deduplicated posts.

    Invalid or cross-group links are ignored. The first occurrence of a post ID
    supplies its content and position, matching its first visible DOM container.
    """

    _require_positive_int(limit, name="post limit")
    _require_positive_int(text_limit, name="text limit")
    accepted_group_keys = {
        key.casefold()
        for key in (
            (group.key,)
            if allowed_group_keys is None
            else (*allowed_group_keys, group.key)
        )
    }

    extracted: list[tuple[int, int, Post]] = []
    seen_post_ids: set[str] = set()

    for source_index, payload in enumerate(payloads):
        if not isinstance(payload, Mapping):
            continue

        link = parse_post_url(payload.get("href"))
        if (
            link is None
            or link.group_key.casefold() not in accepted_group_keys
            or link.post_id in seen_post_ids
        ):
            continue

        position = _payload_position(payload, source_index)
        text = normalize_visible_text(payload.get("text"), limit=text_limit)
        author = normalize_visible_text(
            payload.get("author"),
            limit=MAX_AUTHOR_LENGTH,
        )
        post = Post(
            group_key=group.key,
            post_id=link.post_id,
            url=link.url,
            text=text,
            author=author or None,
            observed_at=observed_at,
            position=position,
            partial=payload.get("partial") is True,
            published_at=parse_facebook_timestamp(
                payload.get("timestamp"),
                observed_at,
                timezone_name=timezone_name,
            ),
        )
        extracted.append((position, source_index, post))
        seen_post_ids.add(link.post_id)

    extracted.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in extracted[:limit])
