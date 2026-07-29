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

        scan = self.source.fetch_recent(group, policy)
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

        if pending:
            for chunk in render_digest_chunks(group.key, pending):
                try:
                    self.sink.send(chunk.notification)
                except DeliveryError as exc:
                    self.state.mark_delivery_failed(
                        chunk.event_ids,
                        str(exc),
                    )
                    raise
                except Exception as exc:
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

        remaining = len(self.state.pending(group))
        return RunSummary(
            group_key=group.key,
            observed=len(scan.posts),
            new_posts=0 if batch.baseline else batch.queued,
            pending=remaining,
            delivered=delivered,
            baseline=batch.baseline,
        )
