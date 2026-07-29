"""Validate Facebook URLs and normalize visible post payloads."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
_RELATIVE_TIMESTAMP_RE = re.compile(
    r"^(?P<amount>[0-9]+)\s*(?P<unit>[smhdw])$",
    re.IGNORECASE,
)
_VERBOSE_TIMESTAMP_RE = re.compile(
    r"^(?P<amount>[0-9]+)\s+"
    r"(?P<unit>seconds?|minutes?|hours?|days?|weeks?)\s+ago$",
    re.IGNORECASE,
)
_DAY_TIME_RE = re.compile(
    r"^(?P<day>today|yesterday)\s+at\s+"
    r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})$",
    re.IGNORECASE,
)
_DATE_TIME_RE = re.compile(
    r"^(?P<day>[0-9]{1,2})\s+(?P<month>[A-Za-z]+)\s+at\s+"
    r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})$",
    re.IGNORECASE,
)
_RELATIVE_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
    "second": "seconds",
    "seconds": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


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
) -> datetime | None:
    """Parse one rendered English Facebook post timestamp conservatively."""

    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("observed_at must be a timezone-aware datetime")
    if not isinstance(value, str):
        return None

    text = normalize_visible_text(value, limit=128)
    if not text:
        return None
    if text.casefold() == "just now":
        return observed_at

    relative = _RELATIVE_TIMESTAMP_RE.fullmatch(text)
    if relative is None:
        relative = _VERBOSE_TIMESTAMP_RE.fullmatch(text)
    if relative is not None:
        amount = int(relative.group("amount"))
        unit = _RELATIVE_UNITS[relative.group("unit").casefold()]
        try:
            return observed_at - timedelta(**{unit: amount})
        except OverflowError:
            return None

    day_time = _DAY_TIME_RE.fullmatch(text)
    if day_time is not None:
        hour = int(day_time.group("hour"))
        minute = int(day_time.group("minute"))
        if hour > 23 or minute > 59:
            return None
        day_offset = 1 if day_time.group("day").casefold() == "yesterday" else 0
        candidate = observed_at - timedelta(days=day_offset)
        return candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)

    date_time = _DATE_TIME_RE.fullmatch(text)
    if date_time is None:
        return None
    month = _MONTHS.get(date_time.group("month").casefold())
    day = int(date_time.group("day"))
    hour = int(date_time.group("hour"))
    minute = int(date_time.group("minute"))
    if month is None or hour > 23 or minute > 59:
        return None
    try:
        candidate = observed_at.replace(
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if candidate > observed_at + timedelta(days=1):
            candidate = candidate.replace(year=candidate.year - 1)
    except ValueError:
        return None
    return candidate


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
            ),
        )
        extracted.append((position, source_index, post))
        seen_post_ids.add(link.post_id)

    extracted.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in extracted[:limit])
