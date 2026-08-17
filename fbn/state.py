"""SQLite-backed seen-post state and durable notification outbox."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import TracebackType

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import ensure_private_directory, resolve_state_file
from .exceptions import ConfigurationError, MonitorInUseError
from .models import GroupRef, ObservationBatch, PendingNotification, Post

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_key TEXT PRIMARY KEY,
    initialized_at TEXT,
    last_success_at TEXT,
    next_eligible_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0)
);

CREATE TABLE IF NOT EXISTS posts (
    group_key TEXT NOT NULL,
    post_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (group_key, post_id),
    FOREIGN KEY (group_key) REFERENCES groups(group_key)
);

CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL,
    post_id TEXT NOT NULL,
    author TEXT,
    body TEXT,
    position INTEGER NOT NULL CHECK (position >= 0),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    UNIQUE (group_key, post_id),
    FOREIGN KEY (group_key, post_id)
        REFERENCES posts(group_key, post_id)
);

CREATE INDEX IF NOT EXISTS outbox_pending_order
    ON outbox (group_key, delivered_at, created_at, position, event_id);

PRAGMA user_version = 1;
"""
_MAX_FUTURE_SKEW = timedelta(minutes=5)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime, field_name: str) -> str:
    return _as_utc(value, field_name).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed, "stored timestamp")


def _is_same_calendar_day(
    post: Post,
    scan_time: datetime,
) -> bool:
    if post.published_at is None:
        return False
    published_at = _as_utc(post.published_at, "published_at")
    age = scan_time - published_at
    local_scan_time = scan_time.astimezone(post.published_at.tzinfo)
    return (
        age >= -_MAX_FUTURE_SKEW and local_scan_time.date() == post.published_at.date()
    )


def _event_id(group_key: str, post_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fbn:{group_key}:{post_id}"))


class SQLiteStateRepository:
    """Persist group observations and retryable notification events in SQLite."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = resolve_state_file(path)
        if not self.path.parent.exists():
            ensure_private_directory(self.path.parent)
        elif not self.path.parent.is_dir():
            raise ConfigurationError("state file parent path is not a directory")
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        try:
            if not self.path.exists():
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                try:
                    descriptor = os.open(self.path, flags, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            if not self.path.is_file():
                raise ConfigurationError(
                    "The state database path must be a regular file."
                )
            self._connection = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
            )
            self._restrict_database_files()
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(_SCHEMA)
            self._restrict_database_files()
        except ConfigurationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise ConfigurationError(
                "The state database could not be opened or initialized."
            ) from exc

    def _restrict_database_files(self) -> None:
        """Keep the database and any SQLite sidecars owner-readable only."""

        if os.name == "nt":
            return
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(0o600)

    def __enter__(self) -> SQLiteStateRepository:
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection; repeated calls are safe."""

        connection = self._connection
        if connection is not None:
            connection.close()
            self._connection = None

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        """Exclusively own one observation and delivery cycle for this state file."""

        lock_path = self.path.with_name(f".{self.path.name}.run.lock")
        lock = FileLock(str(lock_path))
        try:
            lock.acquire(timeout=0)
        except FileLockTimeout as exc:
            raise MonitorInUseError(
                "Another fbn monitor is already using this state database."
            ) from exc

        try:
            if os.name != "nt" and lock_path.exists():
                lock_path.chmod(0o600)
            yield
        finally:
            lock.release()

    def observe(
        self,
        group: GroupRef,
        posts: Sequence[Post],
        *,
        notify_initial: bool = False,
        observed_at: datetime | None = None,
        same_day_only: bool = False,
    ) -> ObservationBatch:
        """Atomically store unseen posts and, when appropriate, outbox rows."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        if not isinstance(notify_initial, bool):
            raise ValueError("notify_initial must be a boolean")
        if not isinstance(same_day_only, bool):
            raise ValueError("same_day_only must be a boolean")
        scan_time = _as_utc(
            self._clock() if observed_at is None else observed_at,
            "observed_at",
        )
        scan_timestamp = _timestamp(scan_time, "observed_at")
        unique_posts = self._unique_posts(group, posts)
        connection = self._require_connection()

        connection.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_group(connection, group.key)
            group_row = connection.execute(
                "SELECT initialized_at FROM groups WHERE group_key = ?",
                (group.key,),
            ).fetchone()
            initialized = bool(group_row["initialized_at"])
            first_non_empty_scan = not initialized and bool(unique_posts)
            baseline = first_non_empty_scan and not notify_initial
            inserted_posts: list[Post] = []
            queued = 0

            for post in unique_posts:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO posts (
                        group_key,
                        post_id,
                        canonical_url,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        group.key,
                        post.post_id,
                        post.url,
                        scan_timestamp,
                        scan_timestamp,
                    ),
                )
                if cursor.rowcount:
                    inserted_posts.append(post)
                else:
                    connection.execute(
                        """
                        UPDATE posts
                        SET canonical_url = ?, last_seen_at = ?
                        WHERE group_key = ? AND post_id = ?
                        """,
                        (
                            post.url,
                            scan_timestamp,
                            group.key,
                            post.post_id,
                        ),
                    )

            if first_non_empty_scan:
                connection.execute(
                    """
                    UPDATE groups
                    SET initialized_at = ?
                    WHERE group_key = ? AND initialized_at IS NULL
                    """,
                    (scan_timestamp, group.key),
                )

            if not baseline:
                for post in inserted_posts:
                    if same_day_only and not _is_same_calendar_day(post, scan_time):
                        continue
                    connection.execute(
                        """
                        INSERT INTO outbox (
                            event_id,
                            group_key,
                            post_id,
                            author,
                            body,
                            position,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _event_id(group.key, post.post_id),
                            group.key,
                            post.post_id,
                            post.author,
                            post.text,
                            post.position,
                            scan_timestamp,
                        ),
                    )
                    queued += 1

            connection.execute(
                """
                UPDATE groups
                SET last_success_at = ?, consecutive_failures = 0
                WHERE group_key = ?
                """,
                (scan_timestamp, group.key),
            )
            pending = self._pending_rows(connection, group.key)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        return ObservationBatch(
            baseline=baseline,
            inserted=len(inserted_posts),
            queued=queued,
            pending=pending,
        )

    def pending(self, group: GroupRef) -> tuple[PendingNotification, ...]:
        """Return undelivered events in deterministic delivery order."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        return self._pending_rows(self._require_connection(), group.key)

    def mark_delivered(
        self,
        event_ids: Sequence[str],
        delivered_at: datetime | None = None,
    ) -> None:
        """Mark events delivered and erase their retained author and body."""

        identifiers = self._event_ids(event_ids)
        if not identifiers:
            return
        delivered_timestamp = _timestamp(
            self._clock() if delivered_at is None else delivered_at,
            "delivered_at",
        )
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """
                UPDATE outbox
                SET delivered_at = ?,
                    author = NULL,
                    body = NULL,
                    last_error = NULL
                WHERE event_id = ? AND delivered_at IS NULL
                """,
                ((delivered_timestamp, event_id) for event_id in identifiers),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def mark_delivery_failed(
        self,
        event_ids: Sequence[str],
        error: str,
    ) -> None:
        """Record a failed delivery while retaining its pending payload."""

        identifiers = self._event_ids(event_ids)
        if not identifiers:
            return
        if not isinstance(error, str):
            raise ValueError("error must be a string")
        safe_error = error[:1000]
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """
                UPDATE outbox
                SET attempts = attempts + 1, last_error = ?
                WHERE event_id = ? AND delivered_at IS NULL
                """,
                ((safe_error, event_id) for event_id in identifiers),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def next_eligible(self, group: GroupRef) -> datetime | None:
        """Return the persisted earliest next-check time for a group."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        row = (
            self._require_connection()
            .execute(
                "SELECT next_eligible_at FROM groups WHERE group_key = ?",
                (group.key,),
            )
            .fetchone()
        )
        if row is None or row["next_eligible_at"] is None:
            return None
        return _parse_timestamp(row["next_eligible_at"])

    def set_next_eligible(
        self,
        group: GroupRef,
        when: datetime | None,
    ) -> None:
        """Persist or clear the earliest next-check time for a group."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        timestamp = None if when is None else _timestamp(when, "when")
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_group(connection, group.key)
            connection.execute(
                "UPDATE groups SET next_eligible_at = ? WHERE group_key = ?",
                (timestamp, group.key),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def record_failure(
        self,
        group: GroupRef,
        *,
        next_eligible_at: datetime,
        at: datetime | None = None,
    ) -> int:
        """Increment the group's transient-failure count and persist backoff."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        failure_time = _as_utc(self._clock() if at is None else at, "at")
        eligible_time = _as_utc(next_eligible_at, "next_eligible_at")
        if eligible_time < failure_time:
            raise ValueError("next_eligible_at cannot be before the failure time")
        eligible_timestamp = _timestamp(eligible_time, "next_eligible_at")
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_group(connection, group.key)
            connection.execute(
                """
                UPDATE groups
                SET consecutive_failures = consecutive_failures + 1,
                    next_eligible_at = ?
                WHERE group_key = ?
                """,
                (eligible_timestamp, group.key),
            )
            row = connection.execute(
                "SELECT consecutive_failures FROM groups WHERE group_key = ?",
                (group.key,),
            ).fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return int(row["consecutive_failures"])

    def consecutive_failures(self, group: GroupRef) -> int:
        """Return the persisted number of consecutive transient failures."""

        if not isinstance(group, GroupRef):
            raise ValueError("group must be a GroupRef")
        row = (
            self._require_connection()
            .execute(
                "SELECT consecutive_failures FROM groups WHERE group_key = ?",
                (group.key,),
            )
            .fetchone()
        )
        return 0 if row is None else int(row["consecutive_failures"])

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("state repository is closed")
        return connection

    @staticmethod
    def _ensure_group(connection: sqlite3.Connection, group_key: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO groups (group_key) VALUES (?)",
            (group_key,),
        )

    @staticmethod
    def _unique_posts(group: GroupRef, posts: Sequence[Post]) -> tuple[Post, ...]:
        unique: list[Post] = []
        seen: set[str] = set()
        for post in posts:
            if not isinstance(post, Post):
                raise ValueError("posts must contain only Post values")
            if post.group_key != group.key:
                raise ValueError(f"post {post.post_id!r} belongs to a different group")
            if post.post_id not in seen:
                seen.add(post.post_id)
                unique.append(post)
        return tuple(unique)

    @staticmethod
    def _event_ids(event_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(event_ids, (str, bytes)):
            raise ValueError("event IDs must be supplied as a sequence")
        identifiers: list[str] = []
        seen: set[str] = set()
        for event_id in event_ids:
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("event IDs must be non-empty strings")
            if event_id not in seen:
                seen.add(event_id)
                identifiers.append(event_id)
        return tuple(identifiers)

    @staticmethod
    def _pending_rows(
        connection: sqlite3.Connection,
        group_key: str,
    ) -> tuple[PendingNotification, ...]:
        rows = connection.execute(
            """
            SELECT
                outbox.event_id,
                outbox.group_key,
                outbox.post_id,
                posts.canonical_url,
                outbox.author,
                outbox.body,
                outbox.position,
                outbox.created_at,
                outbox.attempts
            FROM outbox
            JOIN posts
              ON posts.group_key = outbox.group_key
             AND posts.post_id = outbox.post_id
            WHERE outbox.group_key = ? AND outbox.delivered_at IS NULL
            ORDER BY outbox.created_at, outbox.position, outbox.event_id
            """,
            (group_key,),
        ).fetchall()
        return tuple(
            PendingNotification(
                event_id=row["event_id"],
                group_key=row["group_key"],
                post_id=row["post_id"],
                url=row["canonical_url"],
                author=row["author"],
                body=row["body"],
                position=row["position"],
                created_at=_parse_timestamp(row["created_at"]),
                attempts=row["attempts"],
            )
            for row in rows
        )


StateRepository = SQLiteStateRepository
SQLiteState = SQLiteStateRepository
