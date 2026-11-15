from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from loguru import logger

from fbn.exceptions import DeliveryError
from fbn.logging import configure_logging
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
    def __init__(self) -> None:
        self.boundary_post_id: str | None = None

    def fetch_recent(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        boundary_post_id: str | None = None,
    ) -> ScanResult:
        assert group is GROUP
        self.boundary_post_id = boundary_post_id
        return ScanResult((POST,), "feed", 0, False)


class FakeState:
    def __init__(
        self,
        batch: ObservationBatch,
        *,
        boundary_post_id: str | None = None,
        read_since_boundary: int = 0,
    ) -> None:
        self.batch = batch
        self.boundary_post_id = boundary_post_id
        self.read_since_boundary = read_since_boundary
        self.counted: list[tuple[Post, ...]] = []
        self.current_pending = list(batch.pending)
        self.delivered: list[str] = []
        self.failed: list[tuple[list[str], str]] = []
        self.observe_kwargs: dict[str, object] = {}
        self.lock_events: list[str] = []

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        self.lock_events.append("acquired")
        try:
            yield
        finally:
            self.lock_events.append("released")

    def notification_boundary(self, group: GroupRef) -> str | None:
        assert group is GROUP
        return self.boundary_post_id

    def posts_read_since_boundary(self, group: GroupRef, posts: Sequence[Post]) -> int:
        assert group is GROUP
        self.counted.append(tuple(posts))
        return self.read_since_boundary

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
        self.lock_events.append("marked")
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
    assert "same_day_only" not in state.observe_kwargs


def test_pending_is_delivered_then_marked() -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))
    sink = FakeSink()

    summary = MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())

    assert len(sink.sent) == 1
    assert state.delivered == ["event-123"]
    assert summary.delivered == 1
    assert summary.pending == 0


def test_source_looks_for_the_stored_notification_boundary() -> None:
    state = FakeState(
        ObservationBatch(True, 1, 0, ()),
        boundary_post_id="remembered-post",
    )
    source = FakeSource()

    MonitorService(source, state, FakeSink()).run_once(GROUP, ScanPolicy())

    assert source.boundary_post_id == "remembered-post"


@pytest.mark.parametrize("bounded", [False, True])
def test_only_complete_scans_may_advance_the_notification_boundary(bounded):
    class CoverageSource(FakeSource):
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            return ScanResult((POST,), "feed", 1, bounded)

    state = FakeState(ObservationBatch(False, 1, 0, ()))
    MonitorService(CoverageSource(), state, FakeSink()).run_once(GROUP, ScanPolicy())
    assert state.observe_kwargs.get("preserve_boundary", False) is bounded
    assert "retire_boundary" not in state.observe_kwargs


def test_marker_beyond_the_scan_limit_is_retired_instead_of_preserved():
    class LimitSource(FakeSource):
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            return ScanResult((POST,), "feed", 3, True, True)

    state = FakeState(
        ObservationBatch(False, 1, 0, ()), boundary_post_id="unreachable-post"
    )
    MonitorService(LimitSource(), state, FakeSink()).run_once(GROUP, ScanPolicy())
    assert state.observe_kwargs.get("retire_boundary") is True
    assert "preserve_boundary" not in state.observe_kwargs
    assert state.counted == []


@pytest.mark.parametrize(("read", "retired"), [(9, False), (10, True), (11, True)])
def test_marker_is_retired_once_bounded_checks_together_read_the_scan_limit(
    read, retired
):
    """Checks that each stop early can still prove a marker out of reach."""

    class PagedSource(FakeSource):
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            return ScanResult((POST,), "feed", 3, True)

    policy = ScanPolicy(sample_count=5, max_scrolls=1)
    assert policy.catch_up_post_limit == 10
    state = FakeState(
        ObservationBatch(False, 1, 0, ()),
        boundary_post_id="unreachable-post",
        read_since_boundary=read,
    )
    MonitorService(PagedSource(), state, FakeSink()).run_once(GROUP, policy)
    assert state.counted == [(POST,)]
    assert state.observe_kwargs.get("retire_boundary", False) is retired
    assert state.observe_kwargs.get("preserve_boundary", False) is not retired


@pytest.mark.parametrize(
    ("boundary_post_id", "posts"),
    [(None, (POST,)), (POST.post_id, (POST,)), ("unreachable-post", ())],
)
def test_bounded_scan_without_a_missing_marker_never_counts_read_posts(
    boundary_post_id, posts
):
    """No marker, a marker that was read, or no post to start a new one from."""

    class BoundedSource(FakeSource):
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            return ScanResult(posts, "feed", 3, True)

    state = FakeState(
        ObservationBatch(False, 1, 0, ()),
        boundary_post_id=boundary_post_id,
        read_since_boundary=999,
    )
    MonitorService(BoundedSource(), state, FakeSink()).run_once(GROUP, ScanPolicy())
    assert state.counted == []
    assert state.observe_kwargs.get("preserve_boundary") is True
    assert "retire_boundary" not in state.observe_kwargs


def test_paged_checks_retire_an_unreachable_marker_in_the_stored_state(tmp_path):
    """Three bounded checks of two posts each reach a limit of four."""

    from fbn.state import SQLiteStateRepository
    from tests.test_state import GROUP as STORED
    from tests.test_state import T0, post

    pages = iter(
        [
            (post("b", position=0), post("a", position=1)),
            (post("b", position=0), post("a", position=1)),
            (post("c", position=0), post("b", position=1)),
            (post("d", position=0), post("c", position=1)),
            (post("e", position=0), post("d", position=1)),
        ]
    )

    class PagedSource:
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            posts = next(pages)
            found = any(p.post_id == boundary_post_id for p in posts)
            return ScanResult(posts, "feed", 1, not found)

    class Sink:
        def send(self, notification: object) -> None:
            pass

    policy = ScanPolicy(sample_count=2, max_scrolls=1)
    with SQLiteStateRepository(tmp_path / "state.sqlite3", clock=lambda: T0) as state:
        # The service records each check at the current time, after this.
        state.observe(STORED, (post("marker"),), observed_at=T0.replace(year=2000))
        service = MonitorService(PagedSource(), state, Sink())
        markers = []
        for _ in range(5):
            service.run_once(STORED, policy)
            markers.append(state.notification_boundary(STORED))
        # Reading the same two posts twice proves nothing more. The fourth
        # distinct post above the marker does, and the new marker is then
        # found by the next check like any other.
        assert markers == ["marker", "marker", "marker", "d", "e"]
        assert state.pending(STORED) == ()


def test_scan_result_marker_out_of_reach_is_a_bounded_boolean():
    assert ScanResult((POST,), "feed", 1, True).marker_out_of_reach is False
    with pytest.raises(ValueError, match="marker_out_of_reach must be a boolean"):
        ScanResult((POST,), "feed", 1, True, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires a bounded scan"):
        ScanResult((POST,), "feed", 1, False, True)


def test_run_lock_encloses_fetch_and_delivery() -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))

    class LockAwareSource(FakeSource):
        def fetch_recent(
            self,
            group: GroupRef,
            policy: ScanPolicy,
            *,
            boundary_post_id: str | None = None,
        ) -> ScanResult:
            state.lock_events.append("fetched")
            return super().fetch_recent(
                group,
                policy,
                boundary_post_id=boundary_post_id,
            )

    class LockAwareSink(FakeSink):
        def send(self, notification: object) -> None:
            state.lock_events.append("sent")
            super().send(notification)

    MonitorService(LockAwareSource(), state, LockAwareSink()).run_once(
        GROUP,
        ScanPolicy(),
    )

    assert state.lock_events == [
        "acquired",
        "fetched",
        "sent",
        "marked",
        "released",
    ]


def test_operational_logs_include_counts_but_not_post_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = FakeState(ObservationBatch(False, 1, 1, (PENDING,)))
    sink = FakeSink()
    records: list[dict[str, object]] = []

    def capture_record(message: object) -> None:
        record = message.record.copy()  # type: ignore[attr-defined]
        record["extra"] = record["extra"].copy()
        records.append(record)

    configure_logging(verbose=True)
    capture_id = logger.add(
        capture_record,
        level="DEBUG",
        filter=lambda record: record["extra"].get("service") == "fbn",
    )
    try:
        MonitorService(FakeSource(), state, sink).run_once(GROUP, ScanPolicy())
    finally:
        logger.remove(capture_id)
        logger.remove()
        logger.disable("fbn")

    assert any(
        record["message"] == "Scan completed"
        and record["extra"]["group_key"] == "group"  # type: ignore[index]
        and record["extra"]["page_state"] == "feed"  # type: ignore[index]
        and record["extra"]["post_count"] == 1  # type: ignore[index]
        for record in records
    )
    assert any(record["message"] == "Observation recorded" for record in records)
    assert any(
        record["message"] == "Notification chunk delivery committed"
        and record["extra"]["group_key"] == "group"  # type: ignore[index]
        and record["extra"]["post_count"] == 1  # type: ignore[index]
        for record in records
    )
    output = str(records)
    assert POST.text not in output
    assert POST.url not in output


def test_inserted_but_ineligible_posts_are_not_reported_as_new() -> None:
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
