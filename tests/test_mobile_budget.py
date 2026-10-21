"""Deterministic scan budgets and state safety; no browser or network."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import fbn.mobile as mobile
from fbn.extractor import parse_group_ref
from fbn.models import Post, ScanPolicy
from fbn.monitor import MonitorService
from fbn.state import SQLiteStateRepository

GROUP = parse_group_ref("contract-group")
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def test_layout_manifest_has_executable_regressions_for_each_retained_incident():
    folder = Path(__file__).parent
    cases = json.loads((folder / "mobile_layout_cases.json").read_text())
    tests = "\n".join(path.read_text() for path in folder.glob("test_*.py"))
    assert len({case["case"] for case in cases}) == len(cases)
    assert len([case for case in cases if case["evidence_date"]]) >= 4
    for case in cases:
        assert case["expected_blocks"] and case["outcome"]
        for name in case["tests"]:
            assert "def " + name + "(" in tests


def post(number):
    return Post(
        GROUP.key,
        str(number),
        GROUP.url + f"posts/{number}/",
        "Synthetic body",
        "Synthetic Author",
        NOW,
        0,
        published_at=NOW,
    )


@pytest.mark.parametrize(
    "failure",
    [
        mobile._ScanBudgetExpired,
        mobile.LayoutChangedError,
        mobile.TransientNavigationError,
    ],
)
def test_budget_keeps_verified_work_but_layout_failure_commits_nothing(
    tmp_path, monkeypatch, failure
):
    source = mobile.MobilePostSource(SimpleNamespace())

    def scan(*args):
        accumulated, progress = args[-2:]
        accumulated.append(post(301))
        progress[0] = 2
        raise failure("Synthetic failure")

    monkeypatch.setattr(source, "_scan_mobile_views", scan)

    class Adapter:
        def fetch_recent(self, group, policy, *, boundary_post_id=None):
            return source._scan_mobile(
                None, None, group, policy, frozenset({group.key}), boundary_post_id
            )

    notifications = []
    sink = SimpleNamespace(send=notifications.append)
    with SQLiteStateRepository(tmp_path / "state.sqlite3") as state:
        state.observe(GROUP, [post(300)])
        service = MonitorService(Adapter(), state, sink)
        if failure is not mobile._ScanBudgetExpired:
            with pytest.raises(failure):
                service.run_once(GROUP, ScanPolicy())
            assert not notifications
            assert (
                state._require_connection()
                .execute("SELECT COUNT(*) FROM posts")
                .fetchone()[0]
                == 1
            )
        else:
            summary = service.run_once(GROUP, ScanPolicy())
            assert summary.delivered == 1
            assert len(notifications) == 1
            assert service.run_once(GROUP, ScanPolicy()).delivered == 0
            assert len(notifications) == 1
        assert not state.pending(GROUP)
        assert state.notification_boundary(GROUP) == "300"
        assert mobile._SCAN_DEADLINE.get() is None


def test_each_action_is_capped_by_the_same_scan_deadline(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    token = mobile._SCAN_DEADLINE.set(10.0)
    try:
        assert mobile._remaining_seconds(31.0, "copy_post_link") == 9.0
        error = mobile._layout_failure("synthetic_structure", phase="post_verification")
        assert error._fbn_remaining_scan_ms == 9000
        assert error._fbn_failure_phase == "post_verification"
        clock[0] = 9.5
        assert mobile._remaining_seconds(31.0, "post_navigation") == 0.5
        clock[0] = 10.0
        with pytest.raises(mobile._ScanBudgetExpired):
            mobile._remaining_seconds(31.0, "expand_primary_preview")
    finally:
        mobile._SCAN_DEADLINE.reset(token)


def test_browser_timeout_at_scan_deadline_is_not_a_layout_failure(monkeypatch):
    monkeypatch.setattr(mobile.time, "monotonic", lambda: 10.0)
    token = mobile._SCAN_DEADLINE.set(10.0)
    try:
        with (
            pytest.raises(mobile._ScanBudgetExpired),
            mobile._action_phase("post_navigation"),
        ):
            raise mobile.PlaywrightTimeoutError("Synthetic timeout")
    finally:
        mobile._SCAN_DEADLINE.reset(token)


def test_copy_cleanup_runs_after_scan_expiry_and_restores_interception(monkeypatch):
    monkeypatch.setattr(mobile, "_dismiss_app_prompt", lambda *args: False)
    events = []

    class Control:
        def count(self):
            return 0

        def wait_for(self, **kwargs):
            events.append("wait")

        def click(self, **kwargs):
            if self is close:
                assert mobile._SCAN_DEADLINE.get() is None
                events.append("close")

    copy, close = Control(), Control()
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        get_by_role=lambda *args, name, **kwargs: (
            copy if name == "Copy link" else close
        ),
        evaluate=lambda script: events.append(
            "restore" if script == mobile._CAPTURE_RESTORE_SCRIPT else "install"
        ),
        wait_for_function=lambda *args, **kwargs: (_ for _ in ()).throw(
            mobile._ScanBudgetExpired()
        ),
    )
    menu = SimpleNamespace(click=lambda **kwargs: None)
    token = mobile._SCAN_DEADLINE.set(mobile.time.monotonic() + 10)
    try:
        with pytest.raises(mobile._ScanBudgetExpired):
            mobile.copy_post_link(page, menu, timeout_seconds=5)
        assert events[-1] == "restore" and "close" in events
        assert mobile._SCAN_DEADLINE.get() is not None
    finally:
        mobile._SCAN_DEADLINE.reset(token)


def test_scroll_settle_waits_for_dom_progress_not_a_fixed_sleep():
    waits = []
    page = SimpleNamespace(
        wait_for_function=lambda script, **kwargs: waits.append((script, kwargs))
    )
    mobile.MobilePostSource._wait_for_scroll_progress(
        page, ScanPolicy(settle_seconds=1), [(1, "Body")]
    )
    assert len(waits) == 1
    assert waits[0][1]["arg"] == [(1, "Body")]
    assert 0 < waits[0][1]["timeout"] <= 1000


@pytest.mark.parametrize(
    "source_keys,target_keys,expected",
    [
        ([], [], False),
        (["attachment:1"], [], False),
        (["attachment:1"], ["attachment:2"], False),
        (["attachment:1"], ["attachment:1"], True),
    ],
)
def test_media_only_identity_never_uses_an_image_count(
    source_keys, target_keys, expected
):
    source = {
        "author": "A",
        "text": "",
        "timestampLabel": "2h",
        "mediaCount": 1,
        "mediaKeys": source_keys,
    }
    target = {**source, "mediaKeys": target_keys}
    assert all(mobile._primary_post_comparisons(target, source).values()) is expected
