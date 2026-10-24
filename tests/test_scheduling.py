from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from fbn.config import ScheduleSettings
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    BrowserUnavailableError,
    ConfigurationError,
    DeliveryError,
    LayoutChangedError,
    MonitorInUseError,
    RenderTimeoutError,
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
    activity: list[float] = []

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
        on_activity=activity.append,
    ).run(
        GROUP,
        POLICY,
        notify_initial=True,
        commit_delivery=False,
        stop_event=event,
    )

    assert service.calls == 1
    assert event.waits == [2 * 60 * 60, 2 * 60 * 60]
    assert service.guard_values == [START + timedelta(hours=4)]
    assert state.set_history == [
        START + timedelta(hours=4),
        START + timedelta(hours=4),
    ]
    assert state.next_at == START + timedelta(hours=4)
    assert uniform_calls == [(60 * 60, 3 * 60 * 60)]
    assert service.options == [(True, False)]
    assert summaries == [SUMMARY]
    assert activity == [7200, 784, 7200]


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
            uniform=lambda lower, upper: lower,
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


def test_render_retries_take_seconds_and_keep_one_routine_slot() -> None:
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("one"), RenderTimeoutError("two"), None],
        on_attempt=lambda: clock.advance(2),
    )
    event = FakeEvent(clock, [False, False, True])
    summaries = []
    samples = []

    def fixed_interval(lower, upper):
        samples.append((lower, upper))
        return lower

    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=fixed_interval,
        on_success=summaries.append,
    ).run(GROUP, POLICY, stop_event=event)

    routine_at = START + timedelta(minutes=44)
    assert event.waits == [5, 10, 44 * 60 - 21]
    assert service.calls == 3
    assert service.guard_values == [START + timedelta(minutes=44)] + [routine_at] * 2
    assert state.next_at == routine_at
    assert samples == [(44 * 60, 99 * 60)]
    assert len(state.failure_history) == 1
    assert state.failures == 0
    assert summaries == [SUMMARY]


def test_render_retry_exhaustion_waits_for_the_normal_check_without_growing_delay():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading")] * 8)
    event = FakeEvent(clock, [False] * 7 + [True])
    samples = []

    def fixed_interval(lower, upper):
        samples.append((lower, upper))
        return lower

    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=fixed_interval,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 8
    assert event.waits == [5, 10, 20, 44 * 60 - 35] * 2
    assert state.next_at == START + timedelta(minutes=88)
    assert state.failures == 2
    assert len(state.failure_history) == len(samples) == 2


def test_render_retry_is_skipped_when_a_full_scan_would_crowd_the_routine_slot():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading")])
    event = FakeEvent(clock, [True])
    # Its ordinary scan allowance exceeds the shortest routine interval.
    policy = ScanPolicy(navigation_timeout_seconds=300)

    class SlowPolicyService:
        def run_once(self, group, requested_policy, **kwargs):
            assert requested_policy is policy
            return service.run_once(group, POLICY, **kwargs)

    MonitorLoop(
        SlowPolicyService(),
        state,
        ScheduleSettings(timedelta(minutes=15), timedelta(minutes=15)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, policy, stop_event=event)

    assert service.calls == 1
    assert event.waits == [15 * 60]
    assert state.next_at == START + timedelta(minutes=15)


def test_render_retry_does_not_rebook_a_nearly_due_routine_check():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("loading")],
        on_attempt=lambda: clock.advance(43 * 60),
    )
    event = FakeEvent(clock, [True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 1
    assert event.waits == [60]
    assert state.next_at == START + timedelta(minutes=44)


def test_render_retry_rechecks_the_routine_deadline_after_a_late_wakeup():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading")])

    class LateEvent(FakeEvent):
        def wait(self, timeout=None):
            interrupted = super().wait(timeout)
            if not interrupted:
                clock.advance(43 * 60)
            return interrupted

    event = LateEvent(clock, [False, True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 1
    assert event.waits == [5, 55]
    assert state.next_at == START + timedelta(minutes=44)


def test_render_retry_does_not_overwrite_a_peers_new_routine_slot():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading")])

    class PeerEvent(FakeEvent):
        def wait(self, timeout=None):
            interrupted = super().wait(timeout)
            if not interrupted:
                state.next_at = START + timedelta(minutes=70)
            return interrupted

    event = PeerEvent(clock, [False, True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 1
    assert event.waits == [5, 70 * 60 - 5]
    assert state.next_at == START + timedelta(minutes=70)


@pytest.mark.parametrize("spare_seconds", [-1, 0, 1])
def test_render_retry_requires_strictly_more_than_scan_headroom_and_delay(
    spare_seconds,
):
    clock = FakeClock()
    state = FakeState()
    remaining = 360 + 5 + spare_seconds
    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("loading")],
        on_attempt=lambda: clock.advance(44 * 60 - remaining),
    )
    event = FakeEvent(clock, [True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert event.waits == ([5] if spare_seconds > 0 else [remaining])
    assert service.calls == 1
    assert state.next_at == START + timedelta(minutes=44)


def test_render_retry_yields_to_an_already_due_routine_check():
    clock = FakeClock()
    state = FakeState()
    calls = 0

    def suspend_first_attempt():
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(48 * 60)

    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("loading"), None],
        on_attempt=suspend_first_attempt,
    )
    event = FakeEvent(clock, [True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 2
    assert event.waits == [44 * 60]
    assert state.failure_history == [(START + timedelta(minutes=48),) * 2]
    assert state.next_at == START + timedelta(minutes=92)


def test_render_retry_preserves_a_peer_schedule_changed_during_initial_scan():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("loading")],
        on_attempt=lambda: state.set_next_eligible(
            GROUP, START + timedelta(minutes=70)
        ),
    )
    event = FakeEvent(clock, [True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 1
    assert event.waits == [70 * 60]
    assert state.failure_history == []
    assert state.next_at == START + timedelta(minutes=70)


def test_render_retry_stops_when_shutdown_arrives_as_the_wait_finishes():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading")])

    class CancelEvent(FakeEvent):
        def wait(self, timeout=None):
            super().wait(timeout)
            self._set = True
            return False

    event = CancelEvent(clock, [False])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 1
    assert event.waits == [5]
    assert state.next_at == START + timedelta(minutes=44)


def test_render_retry_cannot_turn_a_rate_limit_into_another_short_retry():
    clock = FakeClock()
    state = FakeState()
    service = FakeService(
        state,
        clock,
        [RenderTimeoutError("loading"), TransientNavigationError("rate limit")],
    )
    event = FakeEvent(clock, [False, True])
    MonitorLoop(
        service,
        state,
        ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
        clock=clock,
        uniform=lambda lower, upper: lower,
    ).run(GROUP, POLICY, stop_event=event)

    assert service.calls == 2
    assert event.waits == [5, 88 * 60]


@pytest.mark.parametrize(
    "error",
    [
        DeliveryError("delivery"),
        ConfigurationError("configuration"),
        LayoutChangedError("unknown layout"),
        AuthenticationRequiredError("login"),
        AccountActionRequiredError("checkpoint"),
        AccessDeniedError("access"),
        BrowserUnavailableError("browser"),
    ],
)
def test_render_retry_does_not_swallow_a_later_nonretryable_error(error):
    clock = FakeClock()
    state = FakeState()
    service = FakeService(state, clock, [RenderTimeoutError("loading"), error])
    event = FakeEvent(clock, [False])

    with pytest.raises(type(error)):
        MonitorLoop(
            service,
            state,
            ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
            clock=clock,
            uniform=lambda lower, upper: lower,
        ).run(GROUP, POLICY, stop_event=event)
    assert event.waits == [5]
    assert service.calls == 2


def test_render_retry_interruption_honors_routine_slot_after_restart(tmp_path):
    from types import SimpleNamespace

    from fbn.mobile import _raise_render_timeout
    from fbn.models import Post, ScanResult
    from fbn.monitor import MonitorService
    from fbn.state import SQLiteStateRepository

    clock = FakeClock()
    path = tmp_path / "state.sqlite3"
    attempts = []
    notifications = []

    def acquire(*args, **kwargs):
        attempts.append(clock())
        if len(attempts) < 3:
            _raise_render_timeout("find_primary_preview")
        return ScanResult(
            (
                Post(
                    GROUP.key,
                    "301",
                    GROUP.url + "posts/301/",
                    "Verified body",
                    "Author",
                    clock(),
                    0,
                ),
            ),
            "feed",
            0,
            False,
        )

    source = SimpleNamespace(fetch_recent=acquire)
    sink = SimpleNamespace(send=notifications.append)
    schedule = ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99))
    with SQLiteStateRepository(path) as state:
        state.observe(GROUP, [], observed_at=START)
        event = FakeEvent(clock, [True])
        MonitorLoop(
            MonitorService(source, state, sink),
            state,
            schedule,
            clock=clock,
            uniform=lambda lower, upper: lower,
        ).run(GROUP, POLICY, stop_event=event)
        assert len(attempts) == 1
        assert event.waits == [5]
        assert state.consecutive_failures(GROUP) == 1
        assert not state.pending(GROUP)
        assert state.notification_boundary(GROUP) is None
        assert not notifications
    with SQLiteStateRepository(path) as state:
        event = FakeEvent(clock, [False, True])
        MonitorLoop(
            MonitorService(source, state, sink),
            state,
            schedule,
            clock=clock,
            uniform=lambda lower, upper: lower,
        ).run(GROUP, POLICY, stop_event=event)
        assert attempts == [START, START + timedelta(minutes=44)]
        assert event.waits == [44 * 60, 5]
        assert state.consecutive_failures(GROUP) == 2
        assert not state.pending(GROUP)
        assert state.notification_boundary(GROUP) is None
        assert not notifications
    with SQLiteStateRepository(path) as state:
        event = FakeEvent(clock, [False, True])
        MonitorLoop(
            MonitorService(source, state, sink),
            state,
            schedule,
            clock=clock,
            uniform=lambda lower, upper: upper,
        ).run(GROUP, POLICY, notify_initial=True, stop_event=event)
        assert attempts == [
            START,
            START + timedelta(minutes=44),
            START + timedelta(minutes=88),
        ]
        assert event.waits == [44 * 60, 99 * 60]
        assert state.consecutive_failures(GROUP) == 0
        assert not state.pending(GROUP)
        assert state.notification_boundary(GROUP) == "301"
        assert len(notifications) == 1


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
            uniform=lambda lower, upper: lower,
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

    assert service.calls == 0
    assert state.next_at is None
