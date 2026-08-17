from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from fbn.config import ScheduleSettings
from fbn.exceptions import (
    ConfigurationError,
    DeliveryError,
    MonitorInUseError,
    TransientNavigationError,
)
from fbn.models import GroupRef, RunSummary, ScanPolicy
from fbn.scheduling import MAX_BACKOFF, MonitorLoop, backoff_for_failure

GROUP = GroupRef("group", "https://www.facebook.com/groups/group/")
POLICY = ScanPolicy()
START = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
SUMMARY = RunSummary("group", 1, 0, 0, 0, False)


class FakeClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeEvent:
    def __init__(
        self,
        clock: FakeClock,
        wait_results: list[bool] | None = None,
        *,
        initially_set: bool = False,
    ) -> None:
        self.clock = clock
        self.wait_results = list(wait_results or [])
        self.waits: list[float] = []
        self._set = initially_set

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None and timeout > 0
        self.waits.append(timeout)
        if not self.wait_results:
            raise AssertionError("scheduler performed an unexpected wait")
        interrupted = self.wait_results.pop(0)
        if interrupted:
            self._set = True
        else:
            self.clock.advance(timeout)
        return interrupted


class FakeState:
    def __init__(
        self,
        *,
        next_at: datetime | None = None,
        failures: int = 0,
    ) -> None:
        self.next_at = next_at
        self.failures = failures
        self.set_history: list[datetime | None] = []
        self.failure_history: list[tuple[datetime, datetime]] = []

    def next_eligible(self, group: GroupRef) -> datetime | None:
        assert group is GROUP
        return self.next_at

    def set_next_eligible(
        self,
        group: GroupRef,
        when: datetime | None,
    ) -> None:
        assert group is GROUP
        self.next_at = when
        self.set_history.append(when)

    def consecutive_failures(self, group: GroupRef) -> int:
        assert group is GROUP
        return self.failures

    def record_failure(
        self,
        group: GroupRef,
        *,
        next_eligible_at: datetime,
        at: datetime | None = None,
    ) -> int:
        assert group is GROUP
        assert at is not None
        self.failures += 1
        self.next_at = next_eligible_at
        self.failure_history.append((at, next_eligible_at))
        return self.failures


class FakeService:
    def __init__(
        self,
        state: FakeState,
        clock: FakeClock,
        outcomes: list[BaseException | None],
        *,
        on_attempt: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.clock = clock
        self.outcomes = list(outcomes)
        self.on_attempt = on_attempt
        self.calls = 0
        self.guard_values: list[datetime | None] = []
        self.options: list[tuple[bool, bool]] = []

    def run_once(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        notify_initial: bool = False,
        commit_delivery: bool = True,
    ) -> RunSummary:
        assert group is GROUP
        assert policy is POLICY
        self.calls += 1
        self.guard_values.append(self.state.next_at)
        self.options.append((notify_initial, commit_delivery))
        if self.on_attempt is not None:
            self.on_attempt()
        if not self.outcomes:
            raise AssertionError("scheduler performed an unexpected attempt")
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome
        self.state.failures = 0
        return SUMMARY


def test_honors_persisted_eligibility_then_persists_guard_and_success_jitter() -> None:
    clock = FakeClock()
    state = FakeState(next_at=START + timedelta(hours=2))
    service = FakeService(state, clock, [None])
    event = FakeEvent(clock, [False, True])
    uniform_calls: list[tuple[float, float]] = []
    summaries: list[RunSummary] = []

    def midpoint(lower: float, upper: float) -> float:
        uniform_calls.append((lower, upper))
        return (lower + upper) / 2

    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(hours=1), timedelta(hours=3)),
        clock=clock,
        uniform=midpoint,
        on_success=summaries.append,
    ).run(
        GROUP,
        POLICY,
        notify_initial=True,
        commit_delivery=False,
        stop_event=event,
    )

    assert service.calls == 1
    assert event.waits == [2 * 60 * 60, 2 * 60 * 60]
    assert service.guard_values == [START + timedelta(hours=3)]
    assert state.set_history == [
        START + timedelta(hours=3),
        START + timedelta(hours=4),
    ]
    assert state.next_at == START + timedelta(hours=4)
    assert uniform_calls == [(60 * 60, 3 * 60 * 60)]
    assert service.options == [(True, False)]
    assert summaries == [SUMMARY]


def test_long_persisted_wait_is_split_into_interruptible_day_chunks() -> None:
    clock = FakeClock()
    state = FakeState(next_at=START + timedelta(days=3))
    service = FakeService(state, clock, [])
    event = FakeEvent(clock, [False, True])

    MonitorLoop(
        service,
        state,
        ScheduleSettings(),
        clock=clock,
    ).run(GROUP, POLICY, stop_event=event)

    assert event.waits == [timedelta(days=1).total_seconds()] * 2
    assert service.calls == 0


def test_pre_attempt_guard_survives_nontransient_crash_and_error_propagates() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [DeliveryError("not retryable")])

    with pytest.raises(DeliveryError, match="not retryable"):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(),
            clock=clock,
        ).run(GROUP, POLICY, stop_event=FakeEvent(clock))

    assert service.calls == 1
    assert service.guard_values == [START + timedelta(hours=1)]
    assert state.next_at == START + timedelta(hours=1)
    assert state.failure_history == []


def test_active_peer_defers_without_changing_shared_schedule() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [MonitorInUseError("already active"), None],
    )
    event = FakeEvent(clock, [False, True])
    schedule = ScheduleSettings(timedelta(minutes=15), timedelta(minutes=15))

    MonitorLoop(
        service,
        state,
        schedule,
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 2
    assert service.guard_values == [
        START + timedelta(minutes=15),
        START + timedelta(minutes=30),
    ]
    assert event.waits == [15 * 60, 15 * 60]
    assert state.set_history == [
        START + timedelta(minutes=15),
        START + timedelta(minutes=30),
        START + timedelta(minutes=30),
    ]
    assert state.failure_history == []


def test_only_transient_navigation_failures_retry_with_exponential_backoff() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [
            TransientNavigationError("one"),
            TransientNavigationError("two"),
            TransientNavigationError("three"),
        ],
    )
    event = FakeEvent(clock, [False, False, True])

    MonitorLoop(
        service,
        state,
        ScheduleSettings(),
        clock=clock,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 3
    assert event.waits == [
        timedelta(hours=value).total_seconds() for value in (1, 2, 4)
    ]
    assert [retry_at for _, retry_at in state.failure_history] == [
        START + timedelta(hours=1),
        START + timedelta(hours=3),
        START + timedelta(hours=7),
    ]
    assert state.failures == 3


def test_transient_backoff_uses_failure_completion_time() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [TransientNavigationError("slow failure")],
        on_attempt=lambda: clock.advance(5 * 60),
    )
    event = FakeEvent(clock, [True])

    MonitorLoop(
        service,
        state,
        ScheduleSettings(),
        clock=clock,
    ).run(GROUP, POLICY, stop_event=event)

    assert state.failure_history == [
        (
            START + timedelta(minutes=5),
            START + timedelta(hours=1, minutes=5),
        )
    ]


def test_backoff_is_capped_at_24_hours_without_large_integer_overflow() -> None:
    schedule = ScheduleSettings(timedelta(minutes=15), timedelta(minutes=15))

    assert backoff_for_failure(schedule, 1) == timedelta(minutes=15)
    assert backoff_for_failure(schedule, 2) == timedelta(minutes=30)
    assert backoff_for_failure(schedule, 100_000) == MAX_BACKOFF


@pytest.mark.parametrize("failure_number", [0, -1, True])
def test_backoff_rejects_invalid_failure_numbers(failure_number: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        backoff_for_failure(ScheduleSettings(), failure_number)


def test_existing_large_failure_count_persists_capped_retry() -> None:
    clock = FakeClock()
    state = FakeState(failures=100_000)
    service = FakeService(
        state,
        clock,
        [TransientNavigationError("still unavailable")],
    )
    event = FakeEvent(clock, [True])

    MonitorLoop(
        service,
        state,
        ScheduleSettings(),
        clock=clock,
    ).run(GROUP, POLICY, stop_event=event)

    assert state.next_at == START + MAX_BACKOFF


def test_pre_set_stop_event_returns_without_state_reads_or_attempts() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [])

    MonitorLoop(
        service,
        state,
        ScheduleSettings(),
        clock=clock,
    ).run(
        GROUP,
        POLICY,
        stop_event=FakeEvent(clock, initially_set=True),
    )

    assert service.calls == 0
    assert state.set_history == []


def test_scheduler_normalizes_aware_clock_values_to_utc() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    clock = FakeClock(START.astimezone(offset))
    state = FakeState()
    service = FakeService(state, clock, [RuntimeError("stop")])

    with pytest.raises(RuntimeError, match="stop"):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(),
            clock=clock,
        ).run(GROUP, POLICY, stop_event=FakeEvent(clock))

    assert state.next_at == START + timedelta(hours=1)
    assert state.next_at.tzinfo is timezone.utc


def test_scheduler_rejects_naive_wall_clock() -> None:
    clock = FakeClock(START.replace(tzinfo=None))
    state = FakeState()
    service = FakeService(state, clock, [])

    with pytest.raises(ValueError, match="timezone-aware"):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(),
            clock=clock,
        ).run(GROUP, POLICY, stop_event=FakeEvent(clock))

    assert service.calls == 0


def test_scheduler_maps_datetime_overflow_to_configuration_error() -> None:
    clock = FakeClock(datetime.max.replace(tzinfo=timezone.utc))
    state = FakeState()
    service = FakeService(state, clock, [])

    with pytest.raises(ConfigurationError, match="datetime range"):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(),
            clock=clock,
        ).run(GROUP, POLICY, stop_event=FakeEvent(clock))

    assert service.calls == 0


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), 99_999.0])
def test_scheduler_rejects_invalid_injected_uniform_results(value: float) -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [None])

    with pytest.raises(ValueError, match="outside schedule bounds"):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(),
            clock=clock,
            uniform=lambda lower, upper: value,
        ).run(GROUP, POLICY, stop_event=FakeEvent(clock))

    assert service.calls == 1
    assert state.next_at == START + timedelta(hours=1)
