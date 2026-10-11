"""Canonical identity and upgrade contracts, using synthetic state only."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from fbn.extractor import extract_posts
from fbn.state import SQLiteStateRepository
from tests.test_state import GROUP, T0, post


def legacy_database(path, *, delivered=True, canonical_exists=False, url=None):
    with SQLiteStateRepository(path) as state:
        state.observe(GROUP, (post("123"),), notify_initial=True)
        if delivered:
            state.mark_delivered([item.event_id for item in state.pending(GROUP)])
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE posts ADD COLUMN fallback_key TEXT")
        connection.execute("PRAGMA user_version=3")
        if canonical_exists:
            connection.execute(
                "INSERT INTO posts SELECT group_key, 'content-old', canonical_url, "
                "first_seen_at,last_seen_at,'visible-old' FROM posts"
            )
            connection.execute(
                "INSERT INTO outbox SELECT 'old-event',group_key,'content-old',"
                "NULL,NULL,position,created_at,created_at,2,NULL FROM outbox"
            )
        else:
            connection.execute("UPDATE posts SET post_id='content-old'")
            connection.execute("UPDATE outbox SET post_id='content-old', attempts=2")
        if url is not None:
            connection.execute("UPDATE posts SET canonical_url=?", (url,))
        connection.execute(
            "UPDATE groups SET notification_boundary_post_id='content-old'"
        )


@pytest.mark.parametrize("canonical_exists", [False, True])
def test_upgrade_rekeys_receipts_and_boundary_without_resending(
    tmp_path, canonical_exists
):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, canonical_exists=canonical_exists)
    for _ in range(2):
        with SQLiteStateRepository(path) as state:
            assert state.notification_boundary(GROUP) == "123"
            batch = state.observe(GROUP, (post("123", text="Different mobile text"),))
            assert batch.inserted == batch.queued == 0
            assert not batch.pending
            connection = state._require_connection()
            assert (
                connection.execute("SELECT post_id FROM posts").fetchall()[0][0]
                == "123"
            )
            assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
            assert "fallback_key" not in {
                row[1] for row in connection.execute("PRAGMA table_info(posts)")
            }
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_upgrade_preserves_pending_body_and_attempts(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False)
    with SQLiteStateRepository(path) as state:
        pending = state.pending(GROUP)
        assert len(pending) == 1
        assert pending[0].post_id == "123"
        assert pending[0].body == "body 123"
        assert pending[0].attempts == 2


def test_delivered_alias_wins_over_pending_canonical_row(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False, canonical_exists=True)
    with SQLiteStateRepository(path) as state:
        assert not state.pending(GROUP)
        row = state._require_connection().execute("SELECT * FROM outbox").fetchone()
        assert row["delivered_at"] and row["body"] is None and row["author"] is None


def test_unresolvable_history_is_archived_and_open_boundary_preserved(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, url=GROUP.url)
    with SQLiteStateRepository(path) as state:
        connection = state._require_connection()
        assert connection.execute("SELECT count(*) FROM posts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        archive = connection.execute(
            "SELECT record_json FROM identity_archive"
        ).fetchone()
        record = json.loads(archive[0])
        assert record["post"]["post_id"] == "content-old"
        assert record["outbox"]["delivered_at"]
        assert state.notification_boundary(GROUP) == "content-old"


def test_unresolvable_pending_upgrade_rolls_back(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False, url=GROUP.url)
    from fbn.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="pending"):
        SQLiteStateRepository(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT post_id FROM posts").fetchone()[0]
            == "content-old"
        )
        assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_identity_is_the_link_id_even_when_text_changes(tmp_path):
    with SQLiteStateRepository(tmp_path / "state.sqlite3") as state:
        state.observe(GROUP, (post("123"),), notify_initial=True)
        state.mark_delivered([item.event_id for item in state.pending(GROUP)])
        batch = state.observe(
            GROUP, (replace(post("123"), text="New emoji or truncation"),)
        )
        assert batch.queued == batch.inserted == 0


@pytest.mark.parametrize(
    "url", [GROUP.url, GROUP.url + "posts/999/", "https://evil.invalid/"]
)
def test_storage_rejects_missing_or_mismatched_canonical_identity(tmp_path, url):
    with (
        SQLiteStateRepository(tmp_path / "state.sqlite3") as state,
        pytest.raises(ValueError, match="identity"),
    ):
        state.observe(GROUP, (post("123", url=url),))


def test_future_schema_is_rejected_without_downgrading(tmp_path):
    path = tmp_path / "state.sqlite3"
    with SQLiteStateRepository(path):
        pass
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=99")
    from fbn.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="newer"):
        SQLiteStateRepository(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_merge_retains_earliest_seen_latest_seen_and_retry_count(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False, canonical_exists=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE posts SET first_seen_at='2025-01-01T00:00:00+00:00', "
            "last_seen_at='2027-01-01T00:00:00+00:00' WHERE post_id='content-old'"
        )
        connection.execute("UPDATE outbox SET attempts=7 WHERE post_id='123'")
    with SQLiteStateRepository(path) as state:
        connection = state._require_connection()
        row = connection.execute("SELECT * FROM posts").fetchone()
        assert row["first_seen_at"].startswith("2025")
        assert row["last_seen_at"].startswith("2027")
        assert connection.execute("SELECT attempts FROM outbox").fetchone()[0] == 7


def test_pending_merge_preserves_latest_retry_error(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False, canonical_exists=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE outbox SET delivered_at=NULL")
        connection.execute(
            "UPDATE outbox SET attempts=7,last_error='DeliveryError' "
            "WHERE post_id='123'"
        )
    with SQLiteStateRepository(path) as state:
        assert len(state.pending(GROUP)) == 1
        row = state._require_connection().execute("SELECT * FROM outbox").fetchone()
        assert row["attempts"] == 7
        assert row["last_error"] == "DeliveryError"


def test_orphaned_outbox_upgrade_fails_without_losing_history(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, delivered=False)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE outbox SET post_id='orphan'")
    from fbn.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="no stored post"):
        SQLiteStateRepository(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT post_id FROM outbox").fetchone()[0] == "orphan"
        )


def test_missing_link_does_not_create_a_temporary_post():
    assert not extract_posts(
        [{"fallback": True, "identity": "Anything", "text": "Body", "href": GROUP.url}],
        GROUP,
        T0,
        limit=1,
    )
