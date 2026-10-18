"""One-time state upgrades; acquisition never uses historical identities."""

from __future__ import annotations

import json
import sqlite3
import uuid

from .exceptions import ConfigurationError
from .extractor import parse_post_url
from .logging import get_logger

SCHEMA_VERSION = 5
LOGGER = get_logger("state")


def migrate(connection: sqlite3.Connection) -> None:
    """Upgrade canonical identities, then repair unreachable archived markers."""

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ConfigurationError("The state database requires a newer FBN version.")
    if version < 4:
        _migrate_canonical(connection)
    if version == SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    recovered = 0
    try:
        groups = connection.execute(
            "SELECT groups.group_key, archive.record_json FROM groups "
            "JOIN identity_archive AS archive ON archive.group_key=groups.group_key "
            "AND archive.previous_post_id=groups.notification_boundary_post_id "
            "WHERE NOT EXISTS (SELECT 1 FROM posts WHERE "
            "posts.group_key=groups.group_key AND "
            "posts.post_id=groups.notification_boundary_post_id)"
        ).fetchall()
        for group in groups:
            archive = json.loads(group["record_json"])
            old_receipt = archive.get("outbox")
            if not old_receipt or not old_receipt.get("delivered_at"):
                continue
            receipts = connection.execute(
                "SELECT outbox.post_id, posts.canonical_url FROM outbox JOIN posts "
                "ON posts.group_key=outbox.group_key AND posts.post_id=outbox.post_id "
                "WHERE outbox.group_key=? AND outbox.delivered_at IS NOT NULL "
                "AND outbox.created_at>=? "
                "ORDER BY outbox.created_at DESC,outbox.position,outbox.event_id",
                (group["group_key"], old_receipt["created_at"]),
            )
            for receipt in receipts:
                link = parse_post_url(receipt["canonical_url"])
                if link is None or link.post_id != receipt["post_id"]:
                    continue
                # Repair once, to an actual delivered post. Future partial scans
                # must retain this same anchor until they reach it and overlap.
                # Keep the original unmappable history in identity_archive.
                connection.execute(
                    "UPDATE groups SET notification_boundary_post_id=? "
                    "WHERE group_key=?",
                    (receipt["post_id"], group["group_key"]),
                )
                recovered += 1
                break
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    if recovered:
        LOGGER.warning(
            "Archived notification marker recovered from delivered canonical receipt",
            recovered_group_count=recovered,
        )


def _migrate_canonical(connection: sqlite3.Connection) -> None:
    """Upgrade atomically, retaining original records for every changed identity.

    Unlinked delivered/baseline history is archived, never guessed. An unresolved
    coverage marker remains an open gap so a bounded scan cannot skip ahead.
    Unlinked pending deliveries require operator repair before an upgrade.
    """

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > 4:
        raise ConfigurationError("The state database requires a newer FBN version.")
    if version == 4:
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)")}
        if "notification_boundary_post_id" not in columns:
            connection.execute(
                "ALTER TABLE groups ADD COLUMN notification_boundary_post_id TEXT"
            )
            connection.execute(
                "UPDATE groups SET notification_boundary_post_id=("
                "SELECT post_id FROM outbox WHERE outbox.group_key=groups.group_key "
                "ORDER BY created_at DESC,position,event_id LIMIT 1)"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS identity_archive ("
            "group_key TEXT NOT NULL,previous_post_id TEXT NOT NULL,"
            "record_json TEXT NOT NULL,PRIMARY KEY(group_key,previous_post_id))"
        )
        posts = [dict(row) for row in connection.execute("SELECT * FROM posts")]
        events = {
            (row["group_key"], row["post_id"]): dict(row)
            for row in connection.execute("SELECT * FROM outbox")
        }
        if events.keys() - {(row["group_key"], row["post_id"]) for row in posts}:
            raise ConfigurationError(
                "State migration stopped: an outbox record has no stored post."
            )
        merged_posts: dict[tuple[str, str], dict] = {}
        merged_events: dict[tuple[str, str], dict] = {}
        for post in posts:
            old_key = (post["group_key"], post["post_id"])
            event = events.get(old_key)
            link = parse_post_url(post["canonical_url"])
            # Redirect-verified group aliases may differ from the configured slug.
            if link is None or link.post_id != post["post_id"]:
                connection.execute(
                    "INSERT OR IGNORE INTO identity_archive VALUES (?,?,?)",
                    (*old_key, json.dumps({"post": post, "outbox": event})),
                )
            if link is None:
                if event is not None and event["delivered_at"] is None:
                    raise ConfigurationError(
                        "State migration stopped: a pending post has no verified "
                        "direct link. Repair its link before upgrading."
                    )
                continue
            key = (post["group_key"], link.post_id)
            canonical = {
                name: post[name]
                for name in ("group_key", "first_seen_at", "last_seen_at")
            }
            canonical.update(post_id=link.post_id, canonical_url=link.url)
            if key in merged_posts:
                previous = merged_posts[key]
                canonical["first_seen_at"] = min(
                    previous["first_seen_at"], canonical["first_seen_at"]
                )
                canonical["last_seen_at"] = max(
                    previous["last_seen_at"], canonical["last_seen_at"]
                )
            merged_posts[key] = canonical
            connection.execute(
                "UPDATE groups SET notification_boundary_post_id=? "
                "WHERE group_key=? AND notification_boundary_post_id=?",
                (link.post_id, post["group_key"], post["post_id"]),
            )
            if event is None:
                continue
            event.update(
                post_id=link.post_id,
                event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fbn:{key[0]}:{key[1]}")),
            )
            if key in merged_events:
                previous = merged_events[key]
                # Either delivery receipt suppresses a duplicate pending alias.
                delivered = previous["delivered_at"] or event["delivered_at"]
                attempts = max(previous["attempts"], event["attempts"])
                last_error = max((previous, event), key=lambda item: item["attempts"])[
                    "last_error"
                ]
                chosen = min((previous, event), key=lambda item: item["created_at"])
                event = dict(chosen)
                event["delivered_at"] = delivered
                event["attempts"] = attempts
                event["last_error"] = last_error
            if event["delivered_at"] is not None:
                event.update(author=None, body=None, last_error=None)
            merged_events[key] = event

        # Rebuild rather than retain the obsolete fingerprint column/index.
        connection.execute("DROP TABLE outbox")
        connection.execute("DROP TABLE posts")
        connection.execute(
            "CREATE TABLE posts (group_key TEXT NOT NULL,post_id TEXT NOT NULL,"
            "canonical_url TEXT NOT NULL,first_seen_at TEXT NOT NULL,"
            "last_seen_at TEXT NOT NULL,PRIMARY KEY(group_key,post_id),"
            "FOREIGN KEY(group_key) REFERENCES groups(group_key))"
        )
        connection.execute(
            "CREATE TABLE outbox (event_id TEXT PRIMARY KEY,group_key TEXT NOT NULL,"
            "post_id TEXT NOT NULL,author TEXT,body TEXT,position INTEGER NOT NULL "
            "CHECK(position>=0),created_at TEXT NOT NULL,delivered_at TEXT,"
            "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),last_error TEXT,"
            "UNIQUE(group_key,post_id),FOREIGN KEY(group_key,post_id) "
            "REFERENCES posts(group_key,post_id))"
        )
        connection.executemany(
            "INSERT INTO posts VALUES (:group_key,:post_id,:canonical_url,"
            ":first_seen_at,:last_seen_at)",
            merged_posts.values(),
        )
        connection.executemany(
            "INSERT INTO outbox VALUES (:event_id,:group_key,:post_id,:author,:body,"
            ":position,:created_at,:delivered_at,:attempts,:last_error)",
            merged_events.values(),
        )
        connection.execute(
            "CREATE INDEX outbox_pending_order ON outbox "
            "(group_key,delivered_at,created_at,position,event_id)"
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConfigurationError("State migration failed its relationship check.")
        connection.execute("PRAGMA user_version=4")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
