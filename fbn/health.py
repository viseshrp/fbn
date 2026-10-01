"""Passive monitor health from a process-owned scheduler lease."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

LEASE_GRACE_SECONDS = 60


class MonitorHealth:
    """Publish deadlines for actual scheduler work, without opening a browser."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self._clock = clock

    @classmethod
    def from_environment(cls) -> MonitorHealth:
        value = os.environ.get("FBN_HEALTH_FILE")
        return cls(Path(value) if value else None)

    def __enter__(self) -> MonitorHealth:
        self.update(60)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)

    def update(self, seconds: float) -> None:
        if self.path is None:
            return
        record = {
            "pid": os.getpid(),
            "deadline": self._clock() + seconds + LEASE_GRACE_SECONDS,
        }
        descriptor, name = tempfile.mkstemp(prefix=".fbn-health-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def monitor_is_healthy(
    path: Path,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Check process liveness and the last published work deadline."""

    try:
        if path.stat().st_size > 1024:
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = record["pid"]
        deadline = record["deadline"]
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline < clock()
        ):
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def main() -> int:
    value = os.environ.get("FBN_HEALTH_FILE")
    return 0 if value and monitor_is_healthy(Path(value)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
