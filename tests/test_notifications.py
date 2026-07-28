from __future__ import annotations

from datetime import datetime, timedelta, timezone

import apprise
import pytest

from fbn.exceptions import DeliveryError
from fbn.models import PendingNotification
from fbn.notifications import (
    MAX_DIGEST_CHARS,
    AppriseSink,
    ConsoleSink,
    Notification,
    render_digest,
    render_digest_chunks,
)

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def pending(
    post_id: str,
    *,
    position: int,
    author: str | None = "Author",
    body: str | None = "Body",
    created_at: datetime = NOW,
) -> PendingNotification:
    return PendingNotification(
        event_id=f"event-{post_id}",
        group_key="group",
        post_id=post_id,
        url=f"https://www.facebook.com/groups/group/posts/{post_id}/",
        author=author,
        body=body or "",
        position=position,
        created_at=created_at,
        attempts=0,
    )


def test_render_digest_is_deterministic_and_plain_text() -> None:
    notification = render_digest(
        "group",
        [
            pending("later", position=2, body="<script>alert(1)</script>"),
            pending("first", position=0, author=None, body=None),
        ],
    )

    assert notification.title == "2 new posts from group"
    assert notification.body.index("/first/") < notification.body.index("/later/")
    assert "Unknown author" in notification.body
    assert "(Visible text unavailable)" in notification.body
    assert "<html" not in notification.body.lower()


def test_render_digest_keeps_older_batches_before_new_feed_positions() -> None:
    notification = render_digest(
        "group",
        [
            pending(
                "new-position-zero",
                position=0,
                created_at=NOW + timedelta(hours=1),
            ),
            pending("older-position-two", position=2),
        ],
    )

    assert notification.body.index("/older-position-two/") < notification.body.index(
        "/new-position-zero/"
    )


def test_render_digest_chunks_include_every_pending_post_once() -> None:
    items = [
        pending(str(index), position=index, body="x" * 4_000) for index in range(50)
    ]

    chunks = render_digest_chunks("group", items)

    assert len(chunks) > 1
    assert [event_id for chunk in chunks for event_id in chunk.event_ids] == [
        item.event_id for item in items
    ]
    assert all(len(chunk.notification.body) <= MAX_DIGEST_CHARS for chunk in chunks)
    for item in items:
        assert sum(item.url in chunk.notification.body for chunk in chunks) == 1
    with pytest.raises(ValueError, match="multiple digest chunks"):
        render_digest("group", items)


def test_console_sink_uses_injected_writer() -> None:
    output: list[str] = []
    ConsoleSink(output.append).send(Notification("Title", "Body"))
    assert output == ["Title\n\nBody"]


class FakeApprise:
    def __init__(
        self,
        *,
        add_result: bool = True,
        notify_result: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.add_result = add_result
        self.notify_result = notify_result
        self.error = error
        self.notify_kwargs: dict[str, object] = {}

    def add(self, url: str) -> bool:
        assert url == "secret://notification-url"
        if self.error:
            raise self.error
        return self.add_result

    def notify(self, **kwargs: object) -> bool:
        self.notify_kwargs = kwargs
        if self.error:
            raise self.error
        return self.notify_result


def test_apprise_sink_delivers_as_plain_text() -> None:
    app = FakeApprise()
    AppriseSink("secret://notification-url", app_factory=lambda: app).send(
        Notification("Title", "<b>literal text</b>")
    )

    assert app.notify_kwargs == {
        "title": "Title",
        "body": "<b>literal text</b>",
        "body_format": apprise.NotifyFormat.TEXT,
    }


@pytest.mark.parametrize(
    ("app", "message"),
    [
        (FakeApprise(add_result=False), "rejected"),
        (FakeApprise(notify_result=False), "did not deliver"),
    ],
)
def test_apprise_false_results_raise(app: FakeApprise, message: str) -> None:
    with pytest.raises(DeliveryError, match=message):
        AppriseSink("secret://notification-url", app_factory=lambda: app).send(
            Notification("Title", "Body")
        )


def test_apprise_exception_is_redacted() -> None:
    app = FakeApprise(error=RuntimeError("secret://notification-url"))
    with pytest.raises(DeliveryError) as raised:
        AppriseSink("secret://notification-url", app_factory=lambda: app).send(
            Notification("Title", "Body")
        )

    assert "secret://notification-url" not in str(raised.value)
    assert "RuntimeError" in str(raised.value)
