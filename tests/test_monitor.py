from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from fbn.exceptions import DeliveryError
from fbn.models import (
    GroupRef,
    ObservationBatch,
    PendingNotification,
    Post,
    ScanPolicy,
    ScanResult,
)
from fbn.monitor import MonitorService
from fbn.notifications import render_digest_chunks

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)
GROUP = GroupRef("group", "https://www.facebook.com/groups/group/")
POST = Post(
    group_key="group",
    post_id="123",
    url="https://www.facebook.com/groups/group/posts/123/",
    text="Post body",
    author="Author",
    observed_at=NOW,
    position=0,
)
PENDING = PendingNotification(
    event_id="event-123",
    group_key="group",
    post_id="123",
    url=POST.url,
    author=POST.author,
    body=POST.text,
    position=0,
    created_at=NOW,
    attempts=0,
)


class FakeSource:
    def fetch_recent(self, group: GroupRef, policy: ScanPolicy) -> ScanResult:
        assert group is GROUP
        return ScanResult((POST,), "feed", 0, False)


class FakeState:
    def __init__(self, batch: ObservationBatch) -> None:
        self.batch = batch
        self.current_pending = list(batch.pending)
        self.delivered: list[str] = []
        self.failed: list[tuple[list[str], str]] = []
        self.observe_kwargs: dict[str, object] = {}

    def observe(self, *args: object, **kwargs: object) -> ObservationBatch:
        self.observe_kwargs = kwargs
        return self.batch

    def pending(self, group: GroupRef) -> tuple[PendingNotification, ...]:
        assert group is GROUP
        return tuple(self.current_pending)

    def mark_delivered(
        self,
        event_ids: Sequence[str],
        delivered_at: datetime | None = None,
    ) -> None:
        self.delivered.extend(event_ids)
        self.current_pending = [
            item for item in self.current_pending if item.event_id not in event_ids
        ]

    def mark_delivery_failed(self, event_ids: Sequence[str], error: str) -> None:
        self.failed.append((list(event_ids), error))


class FakeSink:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        fail_on_call: int = 1,
    ) -> None:
        self.error = error
        self.fail_on_call = fail_on_call
        self.sent = []

    def send(self, notification: object) -> None:
        if self.error and len(self.sent) + 1 == self.fail_on_call:
            raise self.error
        self.sent.append(notification)


def test_baseline_without_pending_does_not_notify() -> None:
    state = FakeState(ObservationBatch(True, 1, 0, ()))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())

    assert summary.baseline is True
    assert summary.new_posts == 0
    assert summary.delivered == 0
    assert sink.sent == []
    assert state.observe_kwargs["max_post_age"] == timedelta(hours=1)


def test_pending_is_delivered_then_marked() -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())

    assert len(sink.sent) == 1
    assert state.delivered == ["event-123"]
    assert summary.delivered == 1
    assert summary.pending == 0


def test_inserted_but_stale_posts_are_not_reported_as_new() -> None:
    state = FakeState(ObservationBatch(False, 2, 0, ()))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(
        GROUP,
        ScanPolicy(),
    )

    assert summary.new_posts == 0
    assert summary.delivered == 0
    assert sink.sent == []


def test_delivery_failure_is_recorded_and_remains_pending() -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))
    sink = FakeSink(DeliveryError("redacted failure"))

    with pytest.raises(DeliveryError, match="redacted"):
        MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())

    assert state.delivered == []
    assert state.failed == [(["event-123"], "redacted failure")]
    assert state.pending(GROUP) == (PENDING,)


def test_dry_run_does_not_mark_delivery() -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(
        GROUP,
        ScanPolicy(),
        commit_delivery=False,
    )

    assert len(sink.sent) == 1
    assert state.delivered == []
    assert summary.delivered == 0
    assert summary.pending == 1


def oversized_pending(count: int = 50) -> tuple[PendingNotification, ...]:
    return tuple(
        PendingNotification(
            event_id=f"event-{index}",
            group_key=GROUP.key,
            post_id=str(index),
            url=f"https://www.facebook.com/groups/group/posts/{index}/",
            author="Author",
            body="x" * 4_000,
            position=index,
            created_at=NOW,
            attempts=0,
        )
        for index in range(count)
    )


def test_oversized_batch_delivers_and_marks_every_chunk() -> None:
    pending = oversized_pending()
    state = FakeState(ObservationBatch(False, len(pending), len(pending), pending))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(
        GROUP,
        ScanPolicy(),
    )

    assert len(sink.sent) > 1
    assert state.delivered == [item.event_id for item in pending]
    assert summary.delivered == len(pending)
    assert summary.pending == 0


def test_later_chunk_failure_marks_only_that_chunk_and_keeps_rest_pending() -> None:
    pending = oversized_pending()
    chunks = render_digest_chunks(GROUP.key, pending)
    state = FakeState(ObservationBatch(False, len(pending), len(pending), pending))
    sink = FakeSink(DeliveryError("redacted later failure"), fail_on_call=2)

    with pytest.raises(DeliveryError, match="redacted later"):
        MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())

    assert state.delivered == list(chunks[0].event_ids)
    assert state.failed == [(list(chunks[1].event_ids), "redacted later failure")]
    remaining = state.pending(GROUP)
    assert [item.event_id for item in remaining] == [
        item.event_id for item in pending if item.event_id not in chunks[0].event_ids
    ]
