"""Immutable values shared by the browser, state, and notification layers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_non_negative(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class GroupRef:
    """A validated Facebook group key and its canonical URL."""

    key: str
    url: str

    def __post_init__(self) -> None:
        _require_non_empty(self.key, "key")
        _require_non_empty(self.url, "url")


@dataclass(frozen=True, slots=True)
class Post:
    """A post extracted from the visible group feed."""

    group_key: str
    post_id: str
    url: str
    text: str
    author: str | None
    observed_at: datetime
    position: int
    partial: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.group_key, "group_key")
        _require_non_empty(self.post_id, "post_id")
        _require_non_empty(self.url, "url")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if self.author is not None and not isinstance(self.author, str):
            raise ValueError("author must be a string or None")
        if not isinstance(self.observed_at, datetime):
            raise ValueError("observed_at must be a datetime")
        _require_non_negative(self.position, "position")
        if not isinstance(self.partial, bool):
            raise ValueError("partial must be a boolean")


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """Hard limits for one browser scan."""

    sample_count: int = 10
    max_scrolls: int = 4
    stagnant_scrolls: int = 2
    navigation_timeout_seconds: float = 30
    settle_seconds: float = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or not 1 <= self.sample_count <= 50
        ):
            raise ValueError("sample_count must be an integer between 1 and 50")
        if (
            isinstance(self.max_scrolls, bool)
            or not isinstance(self.max_scrolls, int)
            or self.max_scrolls < 0
        ):
            raise ValueError("max_scrolls must be a non-negative integer")
        if (
            isinstance(self.stagnant_scrolls, bool)
            or not isinstance(self.stagnant_scrolls, int)
            or self.stagnant_scrolls < 1
        ):
            raise ValueError("stagnant_scrolls must be a positive integer")
        if (
            isinstance(self.navigation_timeout_seconds, bool)
            or not isinstance(self.navigation_timeout_seconds, (int, float))
            or not math.isfinite(self.navigation_timeout_seconds)
            or self.navigation_timeout_seconds <= 0
        ):
            raise ValueError("navigation_timeout_seconds must be positive")
        if (
            isinstance(self.settle_seconds, bool)
            or not isinstance(self.settle_seconds, (int, float))
            or not math.isfinite(self.settle_seconds)
            or self.settle_seconds < 0
        ):
            raise ValueError("settle_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Posts and classified page state returned by one bounded scan."""

    posts: tuple[Post, ...]
    page_state: str
    scrolls: int
    bounded: bool

    def __post_init__(self) -> None:
        if not isinstance(self.posts, tuple) or not all(
            isinstance(post, Post) for post in self.posts
        ):
            raise ValueError("posts must be a tuple of Post values")
        _require_non_empty(self.page_state, "page_state")
        _require_non_negative(self.scrolls, "scrolls")
        if not isinstance(self.bounded, bool):
            raise ValueError("bounded must be a boolean")


@dataclass(frozen=True, slots=True)
class PendingNotification:
    """An undelivered outbox record with the payload needed for retry."""

    event_id: str
    group_key: str
    post_id: str
    url: str
    author: str | None
    body: str
    position: int
    created_at: datetime
    attempts: int

    def __post_init__(self) -> None:
        _require_non_empty(self.event_id, "event_id")
        _require_non_empty(self.group_key, "group_key")
        _require_non_empty(self.post_id, "post_id")
        _require_non_empty(self.url, "url")
        if self.author is not None and not isinstance(self.author, str):
            raise ValueError("author must be a string or None")
        if not isinstance(self.body, str):
            raise ValueError("body must be a string")
        _require_non_negative(self.position, "position")
        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime")
        _require_non_negative(self.attempts, "attempts")


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    """The durable result of applying one scan to the state store."""

    baseline: bool
    inserted: int
    pending: tuple[PendingNotification, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.baseline, bool):
            raise ValueError("baseline must be a boolean")
        _require_non_negative(self.inserted, "inserted")
        if not isinstance(self.pending, tuple) or not all(
            isinstance(item, PendingNotification) for item in self.pending
        ):
            raise ValueError("pending must be a tuple of PendingNotification values")


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Secret-safe counts describing one monitor run."""

    group_key: str
    observed: int
    new_posts: int
    pending: int
    delivered: int
    baseline: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.group_key, "group_key")
        _require_non_negative(self.observed, "observed")
        _require_non_negative(self.new_posts, "new_posts")
        _require_non_negative(self.pending, "pending")
        _require_non_negative(self.delivered, "delivered")
        if not isinstance(self.baseline, bool):
            raise ValueError("baseline must be a boolean")
