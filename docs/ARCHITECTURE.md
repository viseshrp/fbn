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
| `fbn.cli` | Click commands, option/environment mapping, readable operational logging, exit codes. |
| `fbn.logging` | Secret-free Loguru configuration and human-readable service/component context. |
| `fbn.auth` | Bounded secret-file parsing, Facebook-domain filtering, and cookie normalization. |
| `fbn.config` | Platform paths, browser/scan/schedule validation, duration parsing. |
| `fbn.models` | Immutable group, post, scan, observation, delivery, and run-summary values. |
| `fbn.browser` | Persistent browser authentication, locks, cleanup, and page-state classification. |
| `fbn.mobile` | Mobile primary-card parser, New posts sorting, Copy link verification, bounded scrolling. |
| `fbn.migrations` | Atomic schema upgrades and archived historical identity records. |
| `fbn.extractor` | Canonical group/post URL parsing and DOM payload normalization. |
| `fbn.state` | SQLite schema, migrations, baseline/observation transaction, durable outbox. |
| `fbn.notifications` | Plain-text rendering and Apprise/console sinks. |
| `fbn.monitor` | One-check orchestration and long-running error/backoff policy. |
| `fbn.diagnostics` | Read-only browser/path checks with secret-safe output. |
| `fbn.exceptions` | Typed operational failures and stable exit-code mapping. |

## Core interfaces

```python
class PostSource(Protocol):
    def fetch_recent(
        self,
        group: GroupRef,
        policy: ScanPolicy,
        *,
        boundary_post_id: str | None = None,
    ) -> ScanResult: ...


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

### Mobile parsing boundaries

1. `mobile_dom` establishes the card, primary menu and action-footer boundaries;
   `mobile_structure` recognizes the matching author and publication metadata.
2. `mobile_normalize` builds an ordered tree inside each owned body section.
   Unary presentation wrappers can collapse, but branching, action, ARIA, link,
   text and media boundaries remain. The complete owned-section wrapper may
   collapse without crossing sections. Text nodes and block separators retain
   their original order. `display:contents` wrappers do not hide readable children.
   Within an already-owned body, `MContainer` with `data-type="container"` is
   structural layout, not a new region. It follows the same presentation rules;
   section roots, actions, roles, ARIA and unknown component/types stay hard.
3. `mobile_components` runs named discussion, attachment, split-preview, panel,
   expander, media, action-ownership and unknown-control recognizers. Claims record ownership and
   fixed-name outcomes. Conflicting claims, unknown controls, unclaimed meaningful
   text or the 12,000-node/192-depth budget reject the body. Owned discussion is
   opaque, so a large comment tree is neither parsed as content nor expanded.
   Action ownership composes bottom-up: a photo-only clickable shell may inherit
   attachment ownership only when every child is already an attachment or
   transparent presentation. Nearby unclaimed text, empty actions, menus and
   controls prevent that inheritance.
   Photo/video opening buttons require a matching fixed control category and a
   media-only subtree. Media credit cards additionally require one linked credit
   name and accessible/visible publication metadata in a separate action region.
   Profile-picture labels must name that credit exactly before earning ownership.
   Counted album overlays require a media-only mosaic and matching visible and
   accessible counts. Event response labels normalize selected/unselected state;
   no attachment response, picture or album control is clicked.
   Event cards require actual media, exactly one recognized response/open control,
   a date/title pair, and no other controls, links or unowned actions/text. These
   attachment claims precede expander discovery, so attachment captions/buttons
   cannot become the main post's message or See more action. Structural captures
   expose only the fixed control category, never arbitrary accessible labels.
4. `mobile_view.post_view_issues` validates required content/evidence fields and
   completeness before crossing into application code. Expansion and permalink
   checks independently require preserved identity and matching full content.

`mobile_compare.parser_reference` is an explicitly injected rollout/test context,
not a fallback parser. It evaluates trusted reference and candidate code
synchronously on the same page and compares menu ownership, post counts, missing
signals and semantic post fields. Only counts, fixed field names, booleans and
durations leave the comparison. A disagreement blocks acceptance. There is no
default reference, additional navigation, second publisher or retained historical
parser in the package; a rollout supplies its immutable reference separately.

Generated presentation tests vary wrapper depth, card/header/footer placement,
inline text fragmentation, empty decorations and discussion placement. Semantic
post content and identity must remain unchanged. Unknown interactivity and
unclassified text must still reject. These tests complement, rather than replace,
the complete acquisition/state/delivery/restart and copied-link regression suite.

Render-dependent failures share one current-page classifier in `mobile`.
Readiness, sorting, preview expansion (including a replaced/detached menu), and
post verification recheck security/access signals and capture structural counters
before distinguishing a blank, non-interactive loading shell from visible unknown
content. The original action phase is preserved. A changed expansion URL remains
an identity failure even if the destination is blank. No loading classification
accepts a post, clears the notification marker, extends a deadline or retries
inside the browser operation.
An action budget expiring before a browser waiter follows the same classifier;
it cannot skip late security/layout checks. Already-classified errors propagate
without another snapshot or a second classification pass.
Browser-waiter timeouts and pre-wait budget exhaustion use the same rule: a
recognized unchanged page may retry, while missing/unsupported visible structure
stops. A timed-out preview is never accepted as a complete post.
The final rendering snapshot rereads security signals. If a supported view
arrives between the earlier state check and that deadline snapshot, the scan
aborts for backoff; it neither accepts a late view against stale state nor
reports a layout failure with no missing components.

The scheduler owns bounded recovery within a scheduled check. A classified
`RenderTimeoutError` permits at most three additional acquisitions after 5, 10,
and 20 seconds. Each closes its failed context before the interruptible delay
and uses a fresh context for the next acquisition. No browser operation retries
itself or resets an action deadline.

The normal next slot is selected and persisted before acquisition. A render
retry cannot move that slot, including after recovery or exhaustion. Admission
and post-wakeup checks reserve the shared 90-second scan budget, four configured
navigation/render/sort allowances, 120 seconds for browser startup/cleanup, and
a 30-second guard. At the default 30-second navigation timeout this is six
minutes of headroom, plus the retry delay. A late wakeup or changed shared
schedule cancels the retry. When the budget is exhausted, monitoring waits for
the normal slot rather than increasing it to hours or days. Only one logical
failure is counted for the check. A restart honors the normal persisted slot
and skips abandoned local retries instead of issuing an immediate request.

Ordinary successful checks retain their configured completion-to-next-check
interval. Generic network and rate-limit failures remain separate and retain
the existing slower persisted backoff; they never enter the short render loop.
Authentication/checkpoint, unsupported visible layout and post-identity/content
failures still stop. Short retries never weaken ownership, canonical GUID,
completeness or delivery deduplication checks. Failed acquisitions commit nothing.

Mobile body partitioning also recognizes a split Bloks link preview: an owned
linked image is followed, in the same section, by a separate action containing
only a domain and title. Empty media decorations may intervene, but authored
text, discussion, other links or unknown controls cannot. DOM order within that
section, rather than direct-child positions, supports inert wrapper changes.
Known nested discussion menus are excluded at their own discussion boundary.
Only the caption action is excluded, never its
whole section. This keeps attachment text out of the strict preview-prefix and
full source/destination comparisons while preserving authored URLs and wording.

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

`post_id` is parsed from the verified canonical post URL. Schema version 4
rekeys prior direct-link records and merges their outbox receipts; a delivered
receipt suppresses a pending duplicate. Original changed records are kept in
`identity_archive`. Unlinked delivered/baseline history is archived, while an
unlinked pending event blocks migration. Schema version 5 repairs a missing,
archived delivered marker once to the latest later canonical delivery receipt.
It preserves the original archive, never selects pending events, and leaves
the gap open if no such receipt exists. Bounded scans retain the recovered
marker; recovery does not establish the identity of archived posts or their
historical coverage. No active acquisition or observation path generates
temporary IDs or matches posts by their text. Archived unlinked history cannot be reliably
matched to a later observation without additional evidence.

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
it.

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
3. Navigate to the mobile group URL and explicitly select New posts.
4. Classify the response URL/status and visible page state.
5. Read each primary card, copy its own link, and verify the resolved post.
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
  +-- blank shell at render deadline -> TransientNavigationError (back off)
  +-- expected post anchors ---------> Feed (extract)
  +-- explicit empty-feed marker ----> EmptyFeed (successful)
  +-- no recognized state -----------> LayoutChanged (stop)
```

The adapter never treats an unclassified blank page as an empty group and never
retries around a security challenge.

A blank boot shell is distinguished from a changed layout using content-free
counts: no readable text, visible controls/media, mobile scroller, primary menu,
or post candidate. A spinner role alone is insufficient. After the existing
readiness deadline, that shell raises a transient navigation failure, records
private evidence, and leaves the observation uncommitted. The scheduler applies
its existing durable, capped exponential backoff; no immediate reload or new
per-link budget is introduced. Unsupported visible UI and post mismatches still
raise a layout error.

## Extraction boundary

The page returns a minimal list of DOM payloads:

```json
{
  "href": "https://www.facebook.com/groups/example/posts/123/",
  "text": "Visible post text",
  "author": "Optional author",
  "timestamp": "12m",
  "position": 0
}
```

`fbn.extractor` then validates the host/scheme, parses group and post IDs,
removes query/fragment tracking data, normalizes Unicode/whitespace, enforces
text limits, parses the rendered Facebook timestamp with `dateparser`, and
deduplicates IDs while retaining the first visible position. Parsing uses the
scan time as an explicit relative base and the configured IANA timezone. The
Playwright context uses that same timezone so rendering and parsing agree. The mobile parser reads only primary-header metadata, excluding comment and
quoted-post timestamps.
Facebook can redirect a numeric group URL while rendering its post permalinks
under a custom group alias. The browser adapter accepts one such alias only
when it is the sole candidate in the visible group-navigation tablist; related
group links and feed content are never trusted as identity evidence.
Photo-only posts are identified from a strict Facebook photo link containing
both `set=gm.<post-id>` and `idorvanity=<group-id>`, then canonicalized to the
same group-post URL used by state and notifications.

Selectors are kept in one module. Local sanitized fixture tests cover both
`/posts/` and `/permalink/` forms, duplicate/shared links, missing author/text,
pinned/reordered content, and unrecognized layouts.

## SQLite state and outbox

SQLite runs in WAL mode with foreign keys enabled.

All supported unseen posts enter `posts`, even when their publication time is
unavailable. The first non-empty scan stores the top post as a notification
boundary. Later scans look through the allowed scroll passes for that post,
queue each unnotified post above it, and advance the boundary to the newest
queued post. If the stored boundary is not found, every unnotified visible post
is queued but the boundary is retained. This open gap lets later scans queue
posts that were previously beyond the scan limit. Existing pending events
remain retryable across restarts.

```sql
CREATE TABLE groups (
    group_key TEXT PRIMARY KEY,
    initialized_at TEXT,
    last_success_at TEXT,
    next_eligible_at TEXT,
    notification_boundary_post_id TEXT,
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
  authentication export, group ID, and Chromium selection.
- The service then runs
  `fbn monitor --browser chromium --headless ...` without a permanent display.
- The profile remains owner-only. Newly created state parents are owner-only,
  while existing state-parent modes are preserved; the database and sidecars
  remain private across service restarts.
- Optional headed recovery needs a trusted display, but initial bootstrap does
  not.
- ARM64 validation is complete when the sanitized fixture suite
  launches the real Playwright-managed regular Chromium binary headlessly on a
  native Ubuntu ARM64 target, including the Raspberry Pi 4 hardware target.

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

Docker validation includes configuration expansion for the default and
bootstrap profiles, an assertion that only bootstrap receives
`facebook_auth`, native or Buildx builds for both target architectures,
non-root/runtime smoke checks, persistent-volume replacement, and inspection of
the health-check command to prove that it is exactly the package-import probe
and performs no Facebook navigation.
