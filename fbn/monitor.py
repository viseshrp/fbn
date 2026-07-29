"""Application service for one observation and delivery cycle."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

from .exceptions import DeliveryError
from .models import (
    GroupRef,
    ObservationBatch,
    PendingNotification,
    Post,
    RunSummary,
    ScanPolicy,
    ScanResult,
)
from .notifications import NotificationSink, render_digest_chunks
from .structured_logging import get_logger

LOGGER = get_logger("monitor")


class PostSource(Protocol):
    """A bounded source of recent posts."""

    def fetch_recent(self, group: GroupRef, policy: ScanPolicy) -> ScanResult:
        """Fetch recent posts or raise a typed acquisition failure."""


class StateRepository(Protocol):
    """Durable seen-post and notification-outbox operations."""

    def observe(
        self,
        group: GroupRef,
        posts: Sequence[Post],
        *,
        notify_initial: bool = False,
        observed_at: datetime | None = None,
        same_day_only: bool = False,
    ) -> ObservationBatch:
        """Atomically record a scan and return pending delivery records."""

    def pending(self, group: GroupRef) -> tuple[PendingNotification, ...]:
        """Return all currently pending records in deterministic order."""

    def mark_delivered(
        self,
        event_ids: Sequence[str],
        delivered_at: datetime | None = None,
    ) -> None:
        """Mark events delivered and discard retained private body text."""

    def mark_delivery_failed(
        self,
        event_ids: Sequence[str],
        error: str,
    ) -> None:
        """Record a redacted delivery failure without dropping events."""


class MonitorService:
    """Coordinate acquisition, durable observation, and notification."""

    def __init__(
        self,
        source: PostSource,
        state: StateRepository,
        sink: NotificationSink,
    ) -> None:
        self.source = source
        self.state = state
        self.sink = sink

    def run_once(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        notify_initial: bool = False,
        commit_delivery: bool = True,
    ) -> RunSummary:
        """Run one bounded check with at-least-once outbox delivery."""

        LOGGER.info("observation_started", group_key=group.key)
        try:
            scan = self.source.fetch_recent(group, policy)
        except Exception as exc:
            LOGGER.warning(
                "observation_scan_failed",
                group_key=group.key,
                category=type(exc).__name__,
            )
            raise
        LOGGER.info(
            "scan_completed",
            group_key=group.key,
            page_state=scan.page_state,
            post_count=len(scan.posts),
            scroll_count=scan.scrolls,
            bounded=scan.bounded,
        )
        observed_at = datetime.now(timezone.utc)
        batch = self.state.observe(
            group,
            scan.posts,
            notify_initial=notify_initial,
            observed_at=observed_at,
            same_day_only=True,
        )
        pending = batch.pending
        delivered = 0
        LOGGER.info(
            "observation_recorded",
            group_key=group.key,
            baseline=batch.baseline,
            inserted_count=batch.inserted,
            queued_count=batch.queued,
            pending_count=len(pending),
        )

        if pending:
            chunks = render_digest_chunks(group.key, pending)
            LOGGER.info(
                "pending_notifications_delivery_started",
                group_key=group.key,
                post_count=len(pending),
                chunk_count=len(chunks),
                commit_delivery=commit_delivery,
            )
            for index, chunk in enumerate(chunks, start=1):
                LOGGER.debug(
                    "notification_chunk_delivery_started",
                    group_key=group.key,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    post_count=len(chunk.event_ids),
                )
                try:
                    self.sink.send(chunk.notification)
                except DeliveryError as exc:
                    LOGGER.warning(
                        "notification_delivery_failed",
                        group_key=group.key,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        post_count=len(chunk.event_ids),
                        category=type(exc).__name__,
                    )
                    self.state.mark_delivery_failed(
                        chunk.event_ids,
                        str(exc),
                    )
                    raise
                except Exception as exc:
                    LOGGER.warning(
                        "notification_delivery_failed",
                        group_key=group.key,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        post_count=len(chunk.event_ids),
                        category=type(exc).__name__,
                    )
                    redacted = DeliveryError(
                        f"Notification delivery failed ({type(exc).__name__})."
                    )
                    self.state.mark_delivery_failed(
                        chunk.event_ids,
                        str(redacted),
                    )
                    raise redacted from exc

                if commit_delivery:
                    self.state.mark_delivered(
                        chunk.event_ids,
                        delivered_at=observed_at,
                    )
                    delivered += len(chunk.event_ids)
                    LOGGER.info(
                        "notification_chunk_delivery_committed",
                        group_key=group.key,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        post_count=len(chunk.event_ids),
                    )
                else:
                    LOGGER.info(
                        "notification_chunk_dry_run_completed",
                        group_key=group.key,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        post_count=len(chunk.event_ids),
                    )
        else:
            LOGGER.info("no_pending_notifications", group_key=group.key)

        remaining = len(self.state.pending(group))
        summary = RunSummary(
            group_key=group.key,
            observed=len(scan.posts),
            new_posts=0 if batch.baseline else batch.queued,
            pending=remaining,
            delivered=delivered,
            baseline=batch.baseline,
        )
        LOGGER.info(
            "observation_completed",
            group_key=summary.group_key,
            observed=summary.observed,
            new_posts=summary.new_posts,
            delivered=summary.delivered,
            pending=summary.pending,
        )
        return summary
