"""Persistent, interruptible scheduling for long-running monitoring."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Protocol

from .config import ScheduleSettings
from .exceptions import (
    ConfigurationError,
    MonitorInUseError,
    TransientNavigationError,
)
from .logging import get_logger
from .models import GroupRef, RunSummary, ScanPolicy

MAX_BACKOFF = timedelta(hours=24)
MAX_WAIT_SECONDS = MAX_BACKOFF.total_seconds()
LOGGER = get_logger("scheduling")


class EventLike(Protocol):
    """The subset of ``threading.Event`` used by the monitor loop."""

    def is_set(self) -> bool:
        """Return whether shutdown was requested."""

    def wait(self, timeout: float | None = None) -> bool:
        """Wait interruptibly and return whether shutdown was requested."""


class ScheduledMonitor(Protocol):
    """One-check application boundary consumed by the scheduler."""

    def run_once(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        notify_initial: bool = False,
        commit_delivery: bool = True,
    ) -> RunSummary:
        """Perform one browser acquisition, observation, and delivery cycle."""


class ScheduleState(Protocol):
    """Persisted scheduling operations required by the loop."""

    def next_eligible(self, group: GroupRef) -> datetime | None:
        """Return the earliest allowed wall-clock attempt time."""

    def set_next_eligible(
        self,
        group: GroupRef,
        when: datetime | None,
    ) -> None:
        """Persist the earliest allowed wall-clock attempt time."""

    def consecutive_failures(self, group: GroupRef) -> int:
        """Return the current transient-failure count."""

    def record_failure(
        self,
        group: GroupRef,
        *,
        next_eligible_at: datetime,
        at: datetime | None = None,
    ) -> int:
        """Persist one transient failure and its next eligible time."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler clock values must be timezone-aware")
    return value.astimezone(timezone.utc)


def backoff_for_failure(
    schedule: ScheduleSettings,
    failure_number: int,
) -> timedelta:
    """Return lower-bound exponential backoff, capped at 24 hours."""

    if (
        isinstance(failure_number, bool)
        or not isinstance(failure_number, int)
        or failure_number < 1
    ):
        raise ValueError("failure_number must be a positive integer")
    base_seconds = schedule.every.total_seconds()
    exponent = min(failure_number - 1, 63)
    seconds = min(
        MAX_BACKOFF.total_seconds(),
        base_seconds * (2**exponent),
    )
    return timedelta(seconds=seconds)


class MonitorLoop:
    """Run checks indefinitely while honoring persisted scheduling state."""

    def __init__(
        self,
        service: ScheduledMonitor,
        state: ScheduleState,
        schedule: ScheduleSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
        uniform: Callable[[float, float], float] = random.uniform,
        on_success: Callable[[RunSummary], None] | None = None,
    ) -> None:
        self._service = service
        self._state = state
        self._schedule = schedule
        self._clock = clock
        self._uniform = uniform
        self._on_success = on_success

    def run(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        notify_initial: bool = False,
        commit_delivery: bool = True,
        stop_event: EventLike | None = None,
    ) -> None:
        """Run until stopped; retry transient failures and defer to an active peer."""

        stopper = Event() if stop_event is None else stop_event
        LOGGER.info(
            "Scheduler started",
            group_key=group.key,
            interval_min_seconds=int(self._schedule.every.total_seconds()),
            interval_max_seconds=int(self._schedule.to.total_seconds()),
        )
        while not stopper.is_set():
            if not self._wait_until_eligible(group, stopper):
                LOGGER.info("Scheduler stopped before next check", group_key=group.key)
                return

            attempt_started = self._now()
            LOGGER.info("Scheduled check started", group_key=group.key)
            self._state.set_next_eligible(
                group,
                self._add_interval(attempt_started, self._schedule.every),
            )

            try:
                summary = self._service.run_once(
                    group,
                    policy,
                    notify_initial=notify_initial,
                    commit_delivery=commit_delivery,
                )
            except MonitorInUseError:
                next_interval = self._success_interval()
                LOGGER.info(
                    "Scheduled check skipped because another monitor is active",
                    group_key=group.key,
                    delay_seconds=int(next_interval.total_seconds()),
                )
                if stopper.wait(next_interval.total_seconds()):
                    return
                continue
            except TransientNavigationError:
                failed_at = self._now()
                failure_number = self._state.consecutive_failures(group) + 1
                retry_at = self._add_interval(
                    failed_at,
                    backoff_for_failure(
                        self._schedule,
                        failure_number,
                    ),
                )
                self._state.record_failure(
                    group,
                    next_eligible_at=retry_at,
                    at=failed_at,
                )
                LOGGER.warning(
                    "Transient navigation failure",
                    group_key=group.key,
                    failure_number=failure_number,
                    retry_delay_seconds=int((retry_at - failed_at).total_seconds()),
                )
                continue

            LOGGER.info(
                "Scheduled check completed",
                group_key=summary.group_key,
                observed=summary.observed,
                new_posts=summary.new_posts,
                delivered=summary.delivered,
                pending=summary.pending,
                baseline=summary.baseline,
            )
            if self._on_success is not None:
                self._on_success(summary)
            completed_at = self._now()
            next_interval = self._success_interval()
            self._state.set_next_eligible(
                group,
                self._add_interval(completed_at, next_interval),
            )
            LOGGER.info(
                "Next scheduled check",
                group_key=group.key,
                delay_seconds=int(next_interval.total_seconds()),
            )

    def _wait_until_eligible(
        self,
        group: GroupRef,
        stopper: EventLike,
    ) -> bool:
        while not stopper.is_set():
            eligible_at = self._state.next_eligible(group)
            now = self._now()
            if eligible_at is None:
                return True
            delay = (_as_utc(eligible_at) - now).total_seconds()
            if delay <= 0:
                return True
            LOGGER.info(
                "Scheduled check waiting",
                group_key=group.key,
                delay_seconds=math.ceil(delay),
            )
            if stopper.wait(min(delay, MAX_WAIT_SECONDS)):
                return False
        return False

    def _success_interval(self) -> timedelta:
        lower = self._schedule.every.total_seconds()
        upper = self._schedule.to.total_seconds()
        seconds = self._uniform(lower, upper)
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not lower <= seconds <= upper
        ):
            raise ValueError("uniform source returned a value outside schedule bounds")
        return timedelta(seconds=seconds)

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    @staticmethod
    def _add_interval(value: datetime, interval: timedelta) -> datetime:
        try:
            return value + interval
        except OverflowError as exc:
            raise ConfigurationError(
                "The schedule exceeds the supported datetime range."
            ) from exc


Scheduler = MonitorLoop
