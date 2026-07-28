from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fbn.state as state_module
from fbn.models import GroupRef, Post
from fbn.state import SQLiteStateRepository

GROUP = GroupRef(
    key="example-group",
    url="https://www.facebook.com/groups/example-group/",
)
T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def post(
    post_id: str,
    *,
    position: int = 0,
    observed_at: datetime = T0,
    author: str | None = "Author",
    text: str | None = None,
) -> Post:
    return Post(
        group_key=GROUP.key,
        post_id=post_id,
        url=f"https://www.facebook.com/groups/{GROUP.key}/posts/{post_id}/",
        text=f"body {post_id}" if text is None else text,
        author=author,
        observed_at=observed_at,
        position=position,
    )


def test_first_nonempty_scan_is_baseline_even_after_empty_success(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        empty = repository.observe(GROUP, ())
        baseline = repository.observe(
            GROUP,
            (post("one", position=0), post("two", position=1)),
            observed_at=T0 + timedelta(minutes=1),
        )

    assert empty.baseline is False
    assert empty.inserted == 0
    assert empty.pending == ()
    assert baseline.baseline is True
    assert baseline.inserted == 2
    assert baseline.pending == ()


def test_restart_safe_deduplication_queues_only_unseen_posts(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        repository.observe(GROUP, (post("seen"),))

    later = T0 + timedelta(hours=1)
    with SQLiteStateRepository(state_path, clock=lambda: later) as repository:
        observation = repository.observe(
            GROUP,
            (
                post("seen", position=0, observed_at=later),
                post("new", position=1, observed_at=later),
            ),
        )

    assert observation.baseline is False
    assert observation.inserted == 1
    assert [item.post_id for item in observation.pending] == ["new"]


def test_notify_initial_populates_outbox_in_position_order(tmp_path: Path) -> None:
    with SQLiteStateRepository(tmp_path / "state.sqlite3", clock=lambda: T0) as repo:
        observation = repo.observe(
            GROUP,
            (post("later", position=2), post("first", position=0)),
            notify_initial=True,
        )

    assert observation.baseline is False
    assert observation.inserted == 2
    assert [item.post_id for item in observation.pending] == ["first", "later"]


def test_posts_and_outbox_entries_are_inserted_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        repository.observe(GROUP, (post("baseline"),))
        monkeypatch.setattr(state_module, "_event_id", lambda *args: "collision")

        with pytest.raises(sqlite3.IntegrityError):
            repository.observe(
                GROUP,
                (post("new-one", position=0), post("new-two", position=1)),
                observed_at=T0 + timedelta(hours=1),
            )

        rows = (
            repository._require_connection()
            .execute("SELECT post_id FROM posts ORDER BY post_id")
            .fetchall()
        )

    assert [row["post_id"] for row in rows] == ["baseline"]


def test_failed_pending_delivery_survives_restart_and_success_erases_content(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        repository.observe(GROUP, (post("baseline"),))
        pending = repository.observe(
            GROUP,
            (post("new", author="Private author", text="Private body"),),
            observed_at=T0 + timedelta(hours=1),
        ).pending
        event_id = pending[0].event_id
        repository.mark_delivery_failed((event_id,), "temporary notifier error")

    with SQLiteStateRepository(state_path) as repository:
        retried = repository.pending(GROUP)
        assert len(retried) == 1
        assert retried[0].event_id == event_id
        assert retried[0].author == "Private author"
        assert retried[0].body == "Private body"
        assert retried[0].attempts == 1

        repository.mark_delivered(
            (event_id,),
            delivered_at=T0 + timedelta(hours=2),
        )
        assert repository.pending(GROUP) == ()
        row = (
            repository._require_connection()
            .execute(
                """
            SELECT author, body, delivered_at, attempts
            FROM outbox
            WHERE event_id = ?
            """,
                (event_id,),
            )
            .fetchone()
        )

    assert row["author"] is None
    assert row["body"] is None
    assert row["delivered_at"] is not None
    assert row["attempts"] == 1


def test_pending_order_remains_deterministic_across_scans(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        repository.observe(GROUP, (post("baseline"),))
        first_batch = repository.observe(
            GROUP,
            (post("second", position=1), post("first", position=0)),
            observed_at=T0 + timedelta(hours=1),
        )
        second_batch = repository.observe(
            GROUP,
            (post("third", position=0),),
            observed_at=T0 + timedelta(hours=2),
        )

    assert [item.post_id for item in first_batch.pending] == ["first", "second"]
    assert [item.post_id for item in second_batch.pending] == [
        "first",
        "second",
        "third",
    ]


def test_duplicate_posts_are_applied_once_and_first_position_wins(
    tmp_path: Path,
) -> None:
    with SQLiteStateRepository(tmp_path / "state.sqlite3", clock=lambda: T0) as repo:
        observation = repo.observe(
            GROUP,
            (post("same", position=1), post("same", position=0)),
            notify_initial=True,
        )

    assert observation.inserted == 1
    assert len(observation.pending) == 1
    assert observation.pending[0].position == 1


def test_group_mismatch_is_rejected_before_any_state_is_written(
    tmp_path: Path,
) -> None:
    other_post = Post(
        group_key="other",
        post_id="one",
        url="https://www.facebook.com/groups/other/posts/one/",
        text="body",
        author=None,
        observed_at=T0,
        position=0,
    )
    state_path = tmp_path / "state.sqlite3"

    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        with pytest.raises(ValueError, match="different group"):
            repository.observe(GROUP, (other_post,))
        row_count = (
            repository._require_connection()
            .execute("SELECT COUNT(*) FROM groups")
            .fetchone()[0]
        )

    assert row_count == 0


def test_next_eligible_and_failure_count_persist_and_success_resets_failures(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite3"
    eligible = T0 + timedelta(hours=2)
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        assert repository.next_eligible(GROUP) is None
        assert repository.consecutive_failures(GROUP) == 0
        assert (
            repository.record_failure(
                GROUP,
                next_eligible_at=eligible,
            )
            == 1
        )

    with SQLiteStateRepository(state_path, clock=lambda: eligible) as repository:
        assert repository.next_eligible(GROUP) == eligible
        assert repository.consecutive_failures(GROUP) == 1
        repository.observe(GROUP, ())
        assert repository.consecutive_failures(GROUP) == 0
        repository.set_next_eligible(GROUP, None)
        assert repository.next_eligible(GROUP) is None


def test_failure_rejects_a_next_time_before_the_failure(tmp_path: Path) -> None:
    with (
        SQLiteStateRepository(tmp_path / "state.sqlite3", clock=lambda: T0) as repo,
        pytest.raises(ValueError, match="cannot be before"),
    ):
        repo.record_failure(
            GROUP,
            next_eligible_at=T0 - timedelta(seconds=1),
        )


def test_state_file_is_owner_only_on_unix(tmp_path: Path) -> None:
    state_path = tmp_path / "private" / "state.sqlite3"
    with SQLiteStateRepository(state_path):
        pass

    assert state_path.is_file()
    if os.name != "nt":
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert state_path.parent.stat().st_mode & 0o777 == 0o700


def test_existing_state_parent_permissions_are_not_changed(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    if os.name != "nt":
        parent.chmod(0o755)

    state_path = parent / "state.sqlite3"
    with SQLiteStateRepository(state_path, clock=lambda: T0) as repository:
        repository.observe(
            GROUP,
            (post("private", author="Private author", text="Private body"),),
            notify_initial=True,
        )
        if os.name != "nt":
            assert state_path.stat().st_mode & 0o777 == 0o600
            assert (
                state_path.with_name("state.sqlite3-wal").stat().st_mode & 0o777
                == 0o600
            )
            assert (
                state_path.with_name("state.sqlite3-shm").stat().st_mode & 0o777
                == 0o600
            )

    if os.name != "nt":
        assert parent.stat().st_mode & 0o777 == 0o755


def test_missing_database_is_private_before_sqlite_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX file modes are not available")
    state_path = tmp_path / "shared" / "state.sqlite3"
    state_path.parent.mkdir(mode=0o755)
    state_path.parent.chmod(0o755)
    original_connect = sqlite3.connect
    observed_modes: list[int] = []

    def inspected_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed_modes.append(state_path.stat().st_mode & 0o777)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(state_module.sqlite3, "connect", inspected_connect)

    with SQLiteStateRepository(state_path):
        pass

    assert observed_modes == [0o600]


def test_context_manager_closes_repository(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.sqlite3")
    with repository:
        pass

    with pytest.raises(RuntimeError, match="closed"):
        repository.pending(GROUP)
