"""Plain-text notification rendering and delivery sinks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import apprise

from .exceptions import DeliveryError
from .models import PendingNotification

MAX_POST_BODY_CHARS = 4_000
MAX_DIGEST_CHARS = 32_000


@dataclass(frozen=True, slots=True)
class Notification:
    """A transport-neutral plain-text notification."""

    title: str
    body: str


@dataclass(frozen=True, slots=True)
class NotificationChunk:
    """One bounded notification and exactly the outbox events it represents."""

    notification: Notification
    event_ids: tuple[str, ...]


class NotificationSink(Protocol):
    """A destination that either accepts a notification or raises."""

    def send(self, notification: Notification) -> None:
        """Deliver a notification."""


def render_digest(
    group_key: str,
    pending: Sequence[PendingNotification],
) -> Notification:
    """Render one bounded digest; reject input that requires multiple sends."""

    chunks = render_digest_chunks(group_key, pending)
    if len(chunks) > 1:
        raise ValueError("pending notifications require multiple digest chunks")
    return chunks[0].notification


def render_digest_chunks(
    group_key: str,
    pending: Sequence[PendingNotification],
) -> tuple[NotificationChunk, ...]:
    """Render every pending post into deterministic bounded chunks."""

    ordered = sorted(
        pending,
        key=lambda item: (item.created_at, item.position, item.post_id),
    )
    if not ordered:
        return (
            NotificationChunk(
                Notification(f"0 new posts from {group_key}", "No pending posts."),
                (),
            ),
        )

    grouped: list[tuple[list[str], list[str]]] = []
    sections: list[str] = []
    event_ids: list[str] = []
    rendered_length = 0
    for item in ordered:
        author = item.author.strip() if item.author else "Unknown author"
        text = item.body.strip() if item.body else "(Visible text unavailable)"
        text = text[:MAX_POST_BODY_CHARS]
        section = f"{author}\n{text}\n{item.url}"
        added_length = len(section) + (2 if sections else 0)
        if len(section) > MAX_DIGEST_CHARS:
            raise ValueError("one pending notification exceeds the digest limit")
        if sections and rendered_length + added_length > MAX_DIGEST_CHARS:
            grouped.append((sections, event_ids))
            sections = []
            event_ids = []
            rendered_length = 0
            added_length = len(section)
        sections.append(section)
        event_ids.append(item.event_id)
        rendered_length += added_length
    if sections:
        grouped.append((sections, event_ids))

    total_chunks = len(grouped)
    chunks: list[NotificationChunk] = []
    for index, (chunk_sections, chunk_event_ids) in enumerate(grouped, start=1):
        count = len(chunk_event_ids)
        title = f"{count} new post{'s' if count != 1 else ''} from {group_key}"
        if total_chunks > 1:
            title = f"{title} ({index}/{total_chunks})"
        chunks.append(
            NotificationChunk(
                notification=Notification(
                    title=title,
                    body="\n\n".join(chunk_sections),
                ),
                event_ids=tuple(chunk_event_ids),
            )
        )
    return tuple(chunks)


class ConsoleSink:
    """Print notifications without contacting an external service."""

    def __init__(self, writer: Callable[[str], object] = print) -> None:
        self._writer = writer

    def send(self, notification: Notification) -> None:
        self._writer(f"{notification.title}\n\n{notification.body}")


class AppriseSink:
    """Deliver notifications through an Apprise URL."""

    def __init__(
        self,
        url: str,
        *,
        app_factory: Callable[[], object] = apprise.Apprise,
    ) -> None:
        self._url = url
        self._app_factory = app_factory

    def send(self, notification: Notification) -> None:
        try:
            app = self._app_factory()
            if not app.add(self._url):  # type: ignore[attr-defined]
                raise DeliveryError("Apprise rejected the configured notification URL.")
            delivered = app.notify(  # type: ignore[attr-defined]
                title=notification.title,
                body=notification.body,
                body_format=apprise.NotifyFormat.TEXT,
            )
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(
                f"Apprise delivery failed ({type(exc).__name__})."
            ) from exc

        if not delivered:
            raise DeliveryError("Apprise did not deliver the notification.")
