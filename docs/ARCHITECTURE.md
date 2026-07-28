# Architecture

## Overview

`fbn` is a local pipeline with four replaceable boundaries:

```text
Playwright browser -> post extractor -> SQLite state/outbox -> Apprise notifier
                              ^                    ^
                              |                    |
                         scan policy          monitor service
```

The CLI composes these boundaries for headless authentication bootstrap,
optional interactive recovery, one-shot checks, long-running monitoring, and
diagnostics.

The same application pipeline is deployed in two supported Linux forms:

```text
native Ubuntu ARM64 / Raspberry Pi 4
  secret-file bootstrap --\
                          +--> dedicated profile + SQLite state --> headless monitor

Ubuntu 24.04 container (linux/amd64 or linux/arm64)
  /run/secrets bootstrap --\
                           +--> persistent volume ----------> headless monitor
```

Authentication bootstrap and monitoring are headless in both forms. A headed
browser is an optional recovery path only.

## Modules

| Module | Responsibility |
| --- | --- |
| `fbn.cli` | Click commands, option/environment mapping, logging, exit codes. |
| `fbn.auth` | Bounded secret-file parsing, Facebook-domain filtering, and cookie normalization. |
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
    def fetch_recent(self, group: GroupRef, policy: ScanPolicy) -> ScanResult: ...


class StateRepository(Protocol):
    def observe(
        self,
        group: GroupRef,
        posts: Sequence[Post],
        *,
        notify_initial: bool,
    ) -> ObservationBatch: ...

    def pending(self, group: GroupRef) -> Sequence[PendingNotification]: ...

    def mark_delivered(self, event_ids: Sequence[str]) -> None: ...


class NotificationSink(Protocol):
    def send(self, notification: Notification) -> None: ...
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

### Authentication bootstrap

1. Require `--auth-file`, a validated group reference, and the explicit
   automation-risk acknowledgment.
2. Read at most 10 MiB of UTF-8 input and auto-detect a Playwright storage-state
   object, exported-cookie JSON array, or Netscape `cookies.txt`.
3. Normalize cookies, retain only `facebook.com` and subdomain entries, ignore
   storage-state origins, and reject input with no Facebook cookies. No error or
   log contains a cookie name or value.
4. Resolve and create the dedicated profile directory with owner-only
   permissions, acquire its exclusive lock, and enforce the private
   browser-configuration marker.
5. Launch the selected persistent Chromium context headlessly. Ubuntu ARM64 and
   Docker use Playwright-managed regular Chromium with
   `channel="chromium"`.
6. Snapshot the profile's current cookies, replace them with the imported set,
   and navigate to the requested group's chronological feed.
7. Classify the real page. Only an authenticated and accessible group feed is a
   successful bootstrap; no undocumented cookie name is treated as proof.
8. Parser failures occur before the profile is opened. Restore the previous
   cookies on any later browser, authentication, access, or layout failure. On
   success, retain the imported session in the profile.
9. Close the context and release the lock.

The source authentication file is read without modification and is not copied
wholesale into the profile. On Docker, only the profile-gated bootstrap service
receives it as `/run/secrets/facebook_auth`; the monitor service never mounts
it. The import establishes authentication only. It neither extends the
account's authorization nor bypasses a site control.

### Optional headed recovery

`fbn login` opens the same dedicated profile in a headed browser when a user on
a trusted workstation must personally complete login, 2FA, consent, or an
account action. This flow is not required for initial Ubuntu ARM64, Raspberry
Pi, or container bootstrap. Password automation, remote profile upload, and
exposed browser-debugging endpoints remain out of scope.

### Check

1. Acquire the profile lock.
2. Launch the configured persistent context. The default is Playwright Chromium
   headlessly with `channel="chromium"`. `--headed` is an explicit
   troubleshooting override on a trusted display.
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
Facebook can redirect a numeric group URL while rendering its post permalinks
under a custom group alias. The browser adapter accepts one such alias only
when it is the sole candidate in the visible group-navigation tablist; related
group links and feed content are never trusted as identity evidence.

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
User-configured monitor intervals are bounded from 15 minutes through 365 days,
and long waits are split into interruptible 24-hour chunks.
Configuration, authentication, account-action, profile-lock, browser-startup,
access, and layout errors exit immediately with a typed nonzero code.

An expired session is not repaired by the scheduler. The monitor stops and the
user bootstraps a fresh authentication export against the same profile/volume
or uses the optional headed recovery command.

## Security and privacy

- The browser profile and state database live outside the repository.
- On Unix, newly created state parents use `0700`; an existing parent keeps its
  administrator-selected mode. The SQLite database, WAL/SHM sidecars, and lock
  files use `0600`.
- Facebook passwords and cookie values are never accepted in arguments or
  environment variables. `bootstrap` accepts only an explicit file path, and
  cookie names and values are never logged.
- The authentication parser is size-bounded, imports only Facebook domains, and
  ignores Playwright origin storage.
- The source authentication file remains outside the repository and Docker
  build context. The Compose bootstrap service mounts it read-only under
  `/run/secrets`; the monitor service does not receive it.
- An authenticated container volume is treated as credential material: it is
  never baked into an image, uploaded to a registry, or mounted into an
  untrusted container.
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
python -m playwright install --with-deps chromium
```

An installed Chrome/Edge channel needs no Playwright browser download and
remains a desktop option. The required Ubuntu ARM64, Raspberry Pi 4, and Docker
path uses the Playwright-managed browser. `--browser chromium` maps to
`channel="chromium"` for both headless bootstrap and monitoring; an explicit
executable remains an administrator-managed fallback. The same mapping is used
by optional headed recovery.

### Native Ubuntu ARM64 and Raspberry Pi 4

- The host runs 64-bit Ubuntu; Ubuntu 24.04 ARM64 is the deployment baseline.
- Package installation is standard pip installation.
- `python -m playwright install --with-deps chromium` installs the matching
  Chromium build and Ubuntu libraries.
- The user runs fully headless `fbn bootstrap` once with an owner-protected
  authentication export, group ID, Chromium selection, and the explicit risk
  acknowledgment.
- The service then runs
  `fbn monitor --browser chromium --headless ...` without a permanent display.
- The profile remains owner-only. Newly created state parents are owner-only,
  while existing state-parent modes are preserved; the database and sidecars
  remain private across service restarts.
- Optional headed recovery needs a trusted display, but initial bootstrap does
  not.
- A release is not considered ARM64-ready until the sanitized fixture suite
  launches the real Playwright-managed regular Chromium binary headlessly on a
  native Ubuntu ARM64 target, including the Raspberry Pi 4 release target.

### Ubuntu container

The image is built from Ubuntu 24.04 for `linux/amd64` and `linux/arm64`. The
Playwright Python package version and installed browser revision remain aligned.
The image build runs:

```console
python -m playwright install --with-deps chromium
```

The runtime topology is:

```text
private host authentication export
             |
             v
Compose secret /run/secrets/facebook_auth
             |
             v
one-shot non-root fbn bootstrap --browser chromium
             |
             +--> persistent volume/profile/

runtime-only monitor config + Apprise URL
             |
             v
non-root fbn monitor --browser chromium --headless
             |
             +--> Playwright-managed regular Chromium
             |
             +--> persistent volume/
                    +-- profile/       authenticated browser state
                    +-- state.sqlite3  observations and pending delivery
```

The concrete mount path may differ, but profile and state must share an
owner-controlled persistent volume so replacing the container does not force a
new baseline or bootstrap.

The one-shot `bootstrap` Compose service is behind the explicit `bootstrap`
profile. It mounts the source file read-only at
`/run/secrets/facebook_auth`, runs without a display, validates the group, and
writes the resulting browser session to the same volume. The default `fbn`
monitor service has no authentication-secret mount. The monitor must not run
concurrently with bootstrap or optional recovery because the profile lock
permits only one browser context.

The container contract is:

- run as a dedicated non-root UID/GID;
- add no stealth, fingerprint-spoofing, webdriver-hiding, or other
  anti-detection flags;
- mount the authentication export only into the one-shot bootstrap service as a
  read-only `/run/secrets` file, never into the monitor or an environment
  variable;
- inject the Apprise URL and other sensitive configuration only at runtime,
  never through build arguments, image `ENV`, copied `.env` files, or layers;
- keep the bare image default network-inert and use an explicit headless monitor
  command in the deployed Compose service;
- set the monitor restart policy to `no`; the internal scheduler handles
  transient retry/backoff, while hard-stop exits remain stopped;
- persist the profile and SQLite state volume across replacement;
- use an init/termination path that forwards SIGTERM to the monitor; and
- use only `python -c 'import fbn'` for container liveness. The health command
  must not run `fbn check`/`fbn monitor`, launch a browser, navigate to Facebook,
  open the authenticated profile, or claim that the Facebook account/group is
  healthy.

Docker release validation includes configuration expansion for the default and
bootstrap profiles, an assertion that only bootstrap receives
`facebook_auth`, native or Buildx builds for both target architectures,
non-root/runtime smoke checks, persistent-volume replacement, and inspection of
the health-check command to prove that it is exactly the package-import probe
and performs no Facebook navigation.
