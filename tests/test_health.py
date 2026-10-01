from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fbn.health import MonitorHealth, monitor_is_healthy


def test_scheduler_lease_is_private_and_expires_without_progress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.json"
    with MonitorHealth(path, clock=lambda: 100) as health:
        health.update(30)
        assert monitor_is_healthy(path, clock=lambda: 150)
        assert not monitor_is_healthy(path, clock=lambda: 191)
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()
    assert not list(tmp_path.glob(".fbn-health-*"))


def test_failure_removes_scheduler_lease(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    with pytest.raises(RuntimeError), MonitorHealth(path):
        raise RuntimeError("synthetic failure")
    assert not monitor_is_healthy(path)


def test_health_rejects_a_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "health.json"
    with MonitorHealth(path, clock=lambda: 100):

        def dead_process(pid: int, signal: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", dead_process)
        assert not monitor_is_healthy(path, clock=lambda: 110)


@pytest.mark.parametrize(
    "record",
    [
        {},
        [],
        {"pid": True, "deadline": 200},
        {"pid": -1, "deadline": 200},
        {"pid": 1, "deadline": float("nan")},
        {"pid": 1, "deadline": True},
    ],
)
def test_health_rejects_invalid_records(tmp_path: Path, record: object) -> None:
    path = tmp_path / "health.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert not monitor_is_healthy(path, clock=lambda: 100)


def test_health_is_disabled_outside_configured_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FBN_HEALTH_FILE", raising=False)
    with MonitorHealth.from_environment() as health:
        assert health.path is None
        health.update(30)
