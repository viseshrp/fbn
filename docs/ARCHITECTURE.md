# Architecture

## Overview

`fbn` is a local pipeline with four replaceable boundaries:

```text
Playwright browser -> post extractor -> SQLite state/outbox -> Apprise notifier
                              ^                    ^
                              |                    |
                         scan policy          monitor service
```

The CLI composes these boundaries for interactive login, one-shot checks,
long-running monitoring, and diagnostics.

## Modules

| Module | Responsibility |
| --- | --- |
| `fbn.cli` | Click commands, option/environment mapping, logging, exit codes. |
| `fbn.config` | Platform paths, browser/scan/schedule validation, duration parsing. |
| `fbn.models` | Immutable group, post, scan, observation, delivery, and run-summary values. |
| `fbn.browser` | Persistent Playwright context lifecycle, navigation, page-state classification, bounded scrolling. |
| `fbn.extractor` | Canonical group/post URL parsing and DOM payload normalization. |
| `fbn.state` | SQLite schema, migrations, baseline/observation transaction, durable outbox. |
| `fbn.notifications` | Plain-text rendering and Apprise/console sinks. |
| `fbn.monitor` | One-check orchestration and long-running error/backoff policy. |
| `fbn.diagnostics` | Read-only browser/path checks with secret-safe output. |
| `fbn.exceptions` | Typed operational failures and stable exit-code mapping. |

## Core interfaces

```python
class PostSource(Protocol):
    def fetch_recent(self, group: GroupRef, policy: ScanPolicy) -> ScanResult:
        ...


class StateRepository(Protocol):
    def observe(
        self,
        group: GroupRef,
        posts: Sequence[Post],
        *,
        notify_initial: bool,
    ) -> ObservationBatch:
        ...

    def pending(self, group: GroupRef) -> Sequence[PendingNotification]:
        ...

    def mark_delivered(self, event_ids: Sequence[str]) -> None:
        ...


class NotificationSink(Protocol):
    def send(self, notification: Notification) -> None:
        ...
```

`MonitorService.run_once()` is the application boundary. Browser, state, clock,
random source, and notifier dependencies are injected so tests do not contact
Facebook or external notification services.

## Data model

### `GroupRef`

- `key`: validated group ID/slug
- `url`: canonical `https://www.facebook.com/groups/<key>/`

Only Facebook HTTPS group URLs or simple group IDs/slugs are accepted. This is a
domain allowlist and prevents the browser adapter from becoming an arbitrary URL
fetcher.

### `Post`

- `group_key`
- `post_id`
- `url`
- `text`
- `author` (optional)
- `observed_at`
- `position`
- `partial` (the visible DOM may contain collapsed text)

The stable key is `(group_key, post_id)`. Feed position is retained only for
deterministic notification ordering.

### `ScanResult`

- ordered posts
- final classified page state
- number of scrolls
- whether the scan hit the count or stagnation bound

No raw HTML, cookies, screenshot, or browser trace is included.

## Browser lifecycle

### Login

1. Resolve and create the dedicated profile directory with owner-only
   permissions.
2. Acquire an exclusive profile lock.
3. Launch a headed persistent Chromium context.
4. Open `https://www.facebook.com/`.
5. Let the user complete login and security steps.
6. Verify that the browser is no longer on a login/checkpoint page.
7. Close the context and release the lock.

### Check

1. Acquire the profile lock.
2. Launch the configured persistent context.
3. Navigate to the canonical group URL with chronological sorting requested.
4. Classify the response URL/status and visible page state.
5. Extract visible canonical post anchors and semantic container text.
6. Scroll by a viewport at a time while IDs are still increasing and bounds
   remain.
7. Return a normalized `ScanResult`.
8. Close the context in `finally` and release the lock.

Playwright auto-waiting is used for page/locator state. Fixed sleeps are limited
to a small, configurable post-scroll settle window; no artificial human-motion
logic is used.

## Page-state model

```text
navigation
  |
  +-- login URL/form ----------------> AuthenticationRequired (stop)
  +-- checkpoint/consent/CAPTCHA ----> AccountActionRequired (stop)
  +-- HTTP 401/403 ------------------> AccessDenied (stop)
  +-- HTTP 429 / temporary error ----> TransientNavigationError (back off)
  +-- expected post anchors ---------> Feed (extract)
  +-- explicit empty-feed marker ----> EmptyFeed (successful)
  +-- no recognized state -----------> LayoutChanged (stop)
```

The adapter never treats an unclassified blank page as an empty group and never
retries around a security challenge.

## Extraction boundary

The page returns a minimal list of DOM payloads:

```json
{
  "href": "https://www.facebook.com/groups/example/posts/123/",
  "text": "Visible post text",
  "author": "Optional author",
  "position": 0
}
```

`fbn.extractor` then validates the host/scheme, parses group and post IDs,
removes query/fragment tracking data, normalizes Unicode/whitespace, enforces
text limits, and deduplicates IDs while retaining the first visible position.

Selectors are kept in one module. Local sanitized fixture tests cover both
`/posts/` and `/permalink/` forms, duplicate/shared links, missing author/text,
pinned/reordered content, and unrecognized layouts.

## SQLite state and outbox

SQLite runs in WAL mode with foreign keys enabled.

```sql
CREATE TABLE groups (
    group_key TEXT PRIMARY KEY,
    initialized_at TEXT,
    last_success_at TEXT,
    next_eligible_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE posts (
    group_key TEXT NOT NULL,
    post_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (group_key, post_id),
    FOREIGN KEY (group_key) REFERENCES groups(group_key)
);

CREATE TABLE outbox (
    event_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL,
    post_id TEXT NOT NULL,
    author TEXT,
    body TEXT,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (group_key, post_id),
    FOREIGN KEY (group_key, post_id)
        REFERENCES posts(group_key, post_id)
);
```

On a baseline scan, posts are stored without outbox events. On later scans,
previously unseen posts and pending outbox entries are inserted in one
transaction.

Delivery occurs outside the state transaction:

1. read pending outbox rows in deterministic order;
2. render and send one digest;
3. if successful, set `delivered_at` and clear `author`/`body`;
4. if failed, increment attempts and retain content for the next run.

This provides at-least-once delivery without losing a post when Apprise fails.

## Scheduling and failure policy

`check` runs immediately once. External tools can schedule it.

`monitor` uses a monotonic loop:

1. honor any persisted `next_eligible_at`;
2. call `run_once`;
3. choose a cryptographically unnecessary but injectable random interval between
   `--every` and `--to`;
4. persist the next eligible wall-clock time;
5. sleep interruptibly; and
6. stop cleanly on SIGINT/SIGTERM.

Transient navigation failures increase a bounded backoff. A success resets it.
Configuration, authentication, account-action, profile-lock, browser-startup,
access, and layout errors exit immediately with a typed nonzero code.

## Security and privacy

- The browser profile and state database live outside the repository.
- Unix paths use `0700` parent directories and `0600` state/lock files.
- Facebook credentials/cookies are never accepted by the CLI or logged.
- Apprise URLs are read from an option or environment variable but are always
  redacted from errors/logs.
- Extracted post bodies persist only while their notification is pending.
- No telemetry, cloud browser, LLM call, screenshot, trace, or HTML dump is
  enabled.
- Notifications use plain text and canonical links.
- Tests use synthetic local pages and fake notification sinks.

## Packaging and deployment

`pyproject.toml` is the package source of truth. Runtime dependencies are Click,
Apprise, Playwright, `platformdirs`, and a cross-platform profile lock library.
SQLite and scheduling primitives come from the standard library.

Pip installs the Python package. Browser acquisition remains explicit:

```console
python -m playwright install chromium
```

An installed Chrome/Edge channel needs no Playwright browser download. Linux may
require `python -m playwright install --with-deps chromium`. Raspberry Pi users
can pass an installed Chromium executable.

