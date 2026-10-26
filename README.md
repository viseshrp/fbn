# fbn

`fbn` watches the recent visible posts in one Facebook group and sends new-post
notifications through [Apprise](https://github.com/caronc/apprise). Version 0.2
uses Playwright with a dedicated, persistent browser profile instead of direct
mobile-page requests.

This version is an unreleased research tool for local academic use.

## Requirements

- Python 3.10 or newer.
- One supported Chromium-family browser:
  - Playwright Chromium (`--browser chromium`);
  - installed Google Chrome (`--browser chrome`);
  - installed Microsoft Edge (`--browser msedge`); or
  - system Chromium through `--browser executable --executable-path ...`.

`pip` installs the Python package but does not silently download a browser.

## Install

Clone the repository, create a virtual environment if desired, and install the
local checkout:

```console
python -m pip install .
```

Install the default Playwright-managed Chromium build:

```console
python -m playwright install chromium
fbn doctor --browser chromium
```

On a minimal Linux server, Playwright can install Chromium and its operating
system packages:

```console
python -m playwright install --with-deps chromium
```

An existing stable Chrome installation is an optional alternative that needs
no separate browser download:

```console
fbn doctor --browser chrome
```

For a distribution-provided Chromium executable, use:

```console
fbn doctor \
  --browser executable \
  --executable-path /usr/bin/chromium
```

`fbn doctor` is read-only. It reports package, path, and browser readiness
without opening Facebook or printing session or notification secrets.

## Bootstrap authentication once

Export the Facebook cookies from a browser session that already has access to
the group, save them to a private local file, and run:

```console
chmod 600 /private/path/facebook-auth.json

fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chromium
```

`bootstrap` is fully headless and noninteractive. It auto-detects these input
formats:

- a Playwright storage-state JSON object with a top-level `cookies` array;
- a JSON array produced by a cookie exporter; or
- a Netscape `cookies.txt` file.

Only cookies for `facebook.com` or its subdomains are imported. Playwright
storage-state `origins` are ignored. `fbn` does not infer validity from
particular cookie names: it launches the selected browser, opens the requested
group, and requires an authenticated, accessible group feed before bootstrap
succeeds. Cookie names and values are never printed.

Pass authentication material only through `--auth-file`. Do not paste cookie
JSON, cookie values, or a Facebook password into a command, environment
variable, issue, log, or support conversation. Keep the source file outside the
repository and cloud-synced directories. `fbn` reads it without modifying it;
after a successful bootstrap, either remove the source securely or retain it
with owner-only permissions according to your recovery policy.

The dedicated profile is separate from an ordinary Chrome profile so Playwright
does not contend with or modify day-to-day browser state. By default, the
profile and SQLite state file live in the operating system's per-user
application-data directory. They can be overridden with:

```console
export FBN_PROFILE_DIR=/private/path/to/fbn/profile
export FBN_STATE_FILE=/private/path/to/fbn/state.sqlite3
```

The equivalent options are `--profile-dir` and `--state-file`. Keep the profile
private, outside the repository and out of backup or cloud-sync locations that
other people can access. Anyone who obtains it may be able to use the Facebook
session. Only one process may use an `fbn` profile at a time.
Each profile is bound to the browser selection that initialized it. Reuse the
same `--browser` and, for `executable`, the same `--executable-path`; use a
separate profile directory when changing browser configurations.

If the session expires, export fresh authentication data and repeat
`fbn bootstrap` with the same group, browser, and profile settings. On a
workstation with a trusted display, `fbn login` remains available as an
optional headed recovery path for account actions that cannot be completed
headlessly.

## Check once

Start with a dry run:

```console
fbn check \
  --id my-group \
  --browser chromium \
  --dry-run
```

`--id` accepts a Facebook group ID, group slug, or canonical group URL. The
first non-empty successful check records a baseline and does not send a backlog
of existing posts. Use `--notify-initial` only if notifying for the initial
visible sample is intentional.

For real delivery, configure one of
[Apprise's supported notification URLs](https://github.com/caronc/apprise/wiki)
through the environment:

```console
export FBN_APPRISE_URL='mailto://user:app-password@example.com'

fbn check \
  --id my-group \
  --browser chromium
```

Keeping `FBN_APPRISE_URL` out of shell history is safer than passing
`--apprise-url` on the command line. The URL is treated as a secret and redacted
from `fbn` errors. A notification includes visible post text and links, so that
content is sent to the notification service selected in the Apprise URL.

### Mobile reader

`check`, `monitor`, `bootstrap`, and `login` use mobile Facebook. The desktop
parser, reader switch, temporary post IDs, and content-fingerprint matching have
been removed. Existing cookies and notification settings are unchanged.

The mobile reader is a separate parser for the rendered English-language Bloks
layout at `m.facebook.com`, using Playwright's standard Pixel 7 emulation. It
explicitly selects and verifies **New posts**. It reads each main post's body
and ignores quoted posts, comments, replies, and comment previews. Each link
comes from that main post's own **Copy link** menu. The resolved page must have
the same author, body, and publication time and a canonical post URL for the verified
group. A missing or mismatched link stops the check; mobile never substitutes
the group feed or a previous clipboard value.

The post's first action row is the outer content boundary, even when its painting
is deferred. An incomplete row is not replaced with a later reply's action row.
Within that boundary the reader separately identifies authored message blocks,
attachments, interface panels, and discussion. A recognized interest-feedback
panel is excluded before preview expansion and comparison, even when it persists
on the canonical page. Unknown interactive blocks stop the check; a generic
extra-controls count never permits discarding content or reloading to hide it.
Media-opening buttons need a recognized photo/video label and media-only owned
children. Separate media credit/date metadata and image-backed event previews
are attachments only when their complete control/content shape is recognized;
nearby messages, unrelated buttons and inputs cannot inherit that ownership.
Event response labels may include their selected state. A credited profile-picture
control must name the adjacent credit exactly. A photo-album `+N more` overlay
must match its visible count inside a media-only mosaic. None of these attachment
controls is clicked, and their labels never become notification text.
Known attachment dates can be focusable without being actions. Font-icon glyphs
are ignored only within an otherwise verified attachment; authored message text
and ordinary Unicode emoji are unchanged. Explicit handlers and unknown actions
in date metadata still stop the check.
Accessible publication labels are split into a validated date and, when present,
a known audience label. Unknown prefixes, suffixes or extra fields are rejected.
For a truncated message, the reader records the visible text before and after
the single See more control. Expansion must preserve both sides and grow only
between them. The complete destination message must still match exactly.
Rich image previews require media-only branches and a linked heading with a
validated author/date byline. Adjacent authored paragraphs remain message text.
Inline formatting and repeated paragraphs are preserved. Attachment previews,
poll choices, sale-card actions, avatars, and interface images are not message
text. Media-only links require matching stable attachment identifiers rather
than image counts. Link verification waits for the same document to finish
rendering and still requires exact complete message equality and matching author,
publication, group, and canonical post identity.
Failures log the exact failed comparison (author, body, publication time, or
primary-post count), text-part lengths, control counts and partial/media flags,
never private content.

Later checks scroll toward the saved notification marker and read at least one
additional pass after finding it. Known publication times correct visible
reordering; posts tied with the marker's coarse relative time are included
conservatively. A missing marker, unknown time, or exhausted scan limit leaves
the old marker in place so a later check can keep catching up. Scrolling is
bounded by `--max-scrolls`, the catch-up sample limit, and a 90-second scan
budget. Every post action shares that budget in addition to its own limit. If
it expires during a post, only previously verified posts are returned, with the
old marker preserved; the unfinished post is neither saved nor notified. Genuine
layout/identity failures still abort the entire check before observation or
delivery. None of this guarantees recovery of a post Facebook never displays.
Logs distinguish the scan budget, scroll/post limits, idle feed, unknown dates,
and successful marker-plus-overlap completion. Re-rendered cards reuse a link
verification only within that scan, after a fresh Copy link and complete match.

Numeric group IDs may become readable slugs through an HTTP redirect or the
initial group document's history API. The latter is accepted only with
browser-observed main-frame navigation evidence tied to the successful original
group response, collected before sorting or opening post menus. An unrelated
group, child frame, later document, or unproven alias stops the check.

Off-screen cards whose rendering Facebook defers are read after scrolling makes
them available. A visible but incomplete card still stops the check and logs the
missing component names. The permalink reader supports both wrapped cards and
separate header/body/action sections, stopping before comments in either layout.
Feed hydration and sorting use the configured navigation timeout instead of a
five-second cap. Each post link has one shared navigation/rendering budget of
at most 30 seconds. A missing scroller logs fixed structural counts (mobile
screens, total/visible scrollers, post menus, loading indicators, and dialogs),
never page text, links, or account data. A timeout does not trigger a reload.
Each destination opens in a fresh tab that closes immediately after verification,
including on failure, to limit retained browser memory between posts.
The recognized app-install prompt may be dismissed once through its own
**Not now** action after account-state checks. Unknown blocking dialogs stop
the check; no app-install, engagement, message, or account action is clicked.
Scroll settling waits for DOM progress instead of sleeping unconditionally;
`--settle-seconds` bounds an idle wait and never establishes rendering readiness.
Opening a post menu and copying its link share the configured timeout, capped
at 30 seconds. Closing the menu has a separate cleanup budget of at most five
seconds; clipboard interception is always removed, including after a timeout.

Post identity is the permanent post ID in the verified canonical URL, never a
share token or a hash of rendered text. Text changes, emoji labels, and truncation
do not create a new post. Different IDs remain separate even with identical text.

Schema version 4 upgrades saved direct links to canonical IDs and merges their
seen history and outbox receipts atomically. A delivered alias wins over a
pending duplicate. Original changed records are retained in `identity_archive`.
Historical entries without direct links are archived, not assigned guessed IDs;
FBN cannot reliably identify those old entries if Facebook shows them again.
Schema version 5 repairs an archived, unreachable delivered marker once using
the latest later canonical delivery receipt. It keeps that real marker fixed
until catch-up reaches it; the original archive is unchanged. This does not map
the old ID or prove coverage of unmappable historical posts. Without a later
verified delivery receipt, an unresolved marker remains open. An
unresolved pending delivery stops migration so it cannot be silently dropped.
Keep a private database backup before upgrading; rollback requires both the old
image and the old database. The obsolete fingerprint column and index are removed.

State is committed before notification delivery and pending deliveries survive
restarts. Delivery is at least once: a crash after a successful send but before
the state commit may produce a duplicate, but notifier failure does not silently
discard the post.

## Monitor continuously

```console
fbn monitor \
  --id my-group \
  --browser chromium \
  --timezone America/New_York \
  --every 1h \
  --to 3h
```

If `--every` and `--to` are omitted, `monitor` waits a randomized 1–3 hours
between checks. The minimum accepted interval is 15 minutes. Supported units are
`s`, `m`, `h`, `d`, and `w`, although the 15-minute floor still applies.
The maximum accepted interval is 365 days.

Checks and monitoring are headless by default. `--headed` is an explicit
troubleshooting choice on a trusted display. Authentication, account-action,
access-denied, browser-profile,
configuration, and unsupported-layout failures stop the loop with a nonzero
exit code. Recognized loading/stalled-render failures may retry within the same
scheduled check after 5, 10, and 20 seconds. Retries keep the next routine slot
fixed and are skipped if a full scan plus cleanup/guard time cannot fit before
it. Exhaustion waits for that normal slot; restarting also honors the saved slot.
Generic network/rate-limit failures retain their separate slower backoff.

Useful options include:

- `--sample-count`: set the normal number of recent posts checked; the catch-up
  limit is this number multiplied by the allowed extraction passes;
- `--timezone`: set the IANA timezone used to render and interpret Facebook
  timestamps; the default is `UTC`;
- `--notify-initial`: notify for the first visible sample instead of baselining;
- `--include-errors`: notify a concise, redacted operational error; and
- `-v` / `--verbose`: emit secret-free lifecycle and browser diagnostics as
  readable timestamped lines to standard output, without page or cookie dumps.

Use `fbn COMMAND --help` for the complete command-specific options.

Feed/layout and post-link failures automatically save private viewport screenshots
before closing the failing tabs. Find them in `failure-screenshots/` beside the
profile directory (Docker default: `/home/fbn/.local/share/fbn/failure-screenshots/`
inside the persistent data volume). Each timestamped folder contains `feed.png`,
and `post.png` when a post-link tab failed, plus secret-free `metadata.json`.
Version 2 metadata also records the failed action phase, remaining scan budget,
and structural-capture
status. `feed-layout.json` and `post-layout.json` contain sanitized component
ancestry, selected/excluded block roles, visibility, character counts, and fixed
photo/video/event control categories, never the actual labels. These snapshots
contain no post text, authors, URLs, action IDs, HTML, or input values;
each is limited to 1,000 nodes, 20 levels, and 256 KiB, with truncation reported.
The log identifies the folder and whether capture succeeded. Only the latest 10
failure folders are retained, with at most two 5 MiB images per folder.
Directories are owner-only and files are owner-readable/writable. Inputs and
editable fields are masked; images can still contain private group content, so
they are never sent through notifications or included in Git/Docker builds.
Known login/checkpoint failures are excluded for privacy. Screenshot capture is
best effort, takes at most 3 seconds per tab, and never replaces the original
error; a closed or crashed browser may leave only metadata.

## Docker deployment

The included image is based on Ubuntu 24.04, installs Playwright 1.61.0 and its
matching managed Chromium build, and runs `fbn` as a non-root user. The default
image command is `fbn --help`; building or starting the bare image does not open
Facebook.

The Dockerfile builds natively on `linux/amd64` and `linux/arm64`. A Raspberry
Pi 4 must run a 64-bit (`aarch64`) operating system. On any supported host,
Compose builds the image for that host's architecture:

```console
docker compose build
```

To produce a multi-platform OCI archive:

```console
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --output type=oci,dest=/tmp/fbn-multiarch.oci \
  .
```

### Configure Compose

Create a repository-local `.env` file. It is excluded from the Docker build
context and Git, but it is still sensitive:

```dotenv
FBN_GROUP=my-group
FBN_APPRISE_URL=mailto://user:app-password@example.com
FBN_AUTH_FILE=/absolute/private/path/facebook-auth.json
FBN_DATA_VOLUME=fbn-monitor-data
FBN_TIMEZONE=America/New_York
FBN_EVERY=50m
FBN_TO=50m
FBN_UID=1000
FBN_GID=1000
```

Set `FBN_UID` and `FBN_GID` to the output of `id -u` and `id -g` on Linux. This
keeps files in the persistent volume owned by the intended non-root account.
Protect the file before building:

```console
chmod 600 .env
docker compose build
```

Compose refuses to start unless the group, Apprise URL, and authentication-file
path are present. The authentication file must remain outside the repository.
Cookies, browser state, and group content are not baked into the image. Docker
does retain container environment metadata, including the Apprise URL, so only
users trusted with the Docker daemon should be able to inspect the deployment.

### Start bootstrap and monitoring together

The external volume named by `FBN_DATA_VOLUME` is mounted at
`/home/fbn/.local/share/fbn`. It holds both the dedicated browser profile and
SQLite state. Create it once on a new installation; an existing
`fbn-monitor-data` volume is reused by default:

```console
docker volume create fbn-monitor-data
```

Protect the local configuration and authentication export, then start the
Compose application:

```console
chmod 600 .env /absolute/private/path/facebook-auth.json
docker compose up --detach
```

Compose runs the one-shot `bootstrap` service first. It mounts the
authentication export read-only at `/run/secrets/facebook_auth`, validates the
group headlessly, and updates only the persistent profile. The long-running
`fbn` service starts only after bootstrap exits successfully. The source file
is not placed in an environment variable, copied into the image, or mounted
into the monitor service. Bootstrap requires no display, X11, VNC, or browser
debugging port.

The Compose command is explicitly
`fbn monitor --browser chromium --headless --verbose --include-errors ...`.
Hard failures send one secret-free notification with the failure category and
the appropriate recovery step. Each service has a 1 GiB memory limit with
container swap disabled, and Chromium has a 1 GiB shared memory allocation.
Bootstrap finishes before the monitor starts, so only one browser session is
active. `init: true` forwards termination cleanly.
The monitor writes human-readable Loguru records to standard output, so the
`docker compose logs --follow` shows bootstrap, startup, waits, scan
counts, delivery counts, and retry categories as they happen. Each line has a
UTC timestamp, level, component, plain-language event, and safe `key=value`
context. Non-interactive container output contains no ANSI color codes. Records
never include page text, cookie values, authentication-file paths, or Apprise
URLs. The monitor has no authentication-file secret mount. Both services allow
two minutes for graceful termination so bootstrap has time to verify cookie
rollback and the monitor can close Chromium.

The monitor restart policy is deliberately `no`. Its internal scheduler already
backs off transient navigation failures; authentication, checkpoint, access,
profile, and layout hard stops must leave the container stopped instead of
causing a rapid Docker restart loop. Inspect the logs, resolve the condition,
replace the authentication export when required, and run
`docker compose up --detach` again. If startup after a host reboot
is required, configure it explicitly without an automatic failure restart.
Stop the container without deleting its profile/state volume:

```console
docker compose down
```

Do not add `--volumes` unless permanently deleting the authenticated profile and
deduplication history is intentional. The image `HEALTHCHECK` runs
`python -m fbn.health`. The scheduler publishes a private, container-local lease
before each bounded check and scheduled wait. Health becomes false when the
monitor process dies or its work deadline expires. A normal scheduled wait,
including transient backoff, remains healthy. This check never opens a browser,
reads the profile, sends a notification, or contacts Facebook. Bootstrap has no
healthcheck because it is a one-shot command.

### Supervise Compose with systemd

After a successful bootstrap, `deploy/fbn.service` supervises the monitor in a
checkout at `/opt/fbn-compose`:

```console
sudo install -m 644 deploy/fbn.service /etc/systemd/system/fbn.service
sudo systemctl daemon-reload
sudo systemctl enable --now fbn.service
systemctl status fbn.service
```

The unit runs [Compose in the foreground](https://docs.docker.com/reference/cli/docker/compose/up/)
and returns the monitor's exit status, so a hard failure appears as `failed`
instead of a one-shot unit remaining `active (exited)`. Unexpected process
failures restart after 15 minutes, with a limit of three starts per hour.
Known configuration, browser, authentication, account-action, access, layout,
delivery, and profile errors remain stopped using
[`RestartPreventExitStatus`](https://man7.org/linux/man-pages/man5/systemd.service.5.html).
Resolve the reported condition before `sudo systemctl restart fbn.service`.

The unit deliberately starts only the monitor with `--no-deps`. It reuses the
authenticated profile and does not reimport a potentially old cookie export
after every reboot. Run bootstrap explicitly when fresh authentication is
needed. Stop a running detached Compose monitor before enabling this unit;
afterward, use systemd to start and stop the application.
The unit explicitly selects `compose.yaml`. The legacy `docker-compose.yml`
filename links to that same file, so an existing
`COMPOSE_FILE=docker-compose.yml` override uses the current monitor settings.

## Run checks with a systemd user timer

A timer that launches `fbn check` is safer than an always-restarting service:
each browser session is bounded, systemd prevents overlapping activations, and
the persistent SQLite state handles restarts. Bootstrap the profile first as
the same Unix user that owns the timer.

The following example assumes a virtual environment at
`/home/alice/.local/venvs/fbn` and the default Linux data directory. Substitute
your actual user, group, and paths.

Create `~/.config/fbn/fbn.env`:

```dotenv
FBN_APPRISE_URL=mailto://user:app-password@example.com
FBN_PROFILE_DIR=/home/alice/.local/share/fbn/profile
FBN_STATE_FILE=/home/alice/.local/share/fbn/state.sqlite3
```

Protect the environment file:

```console
chmod 600 ~/.config/fbn/fbn.env
```

Store an authentication export outside the repository, then bootstrap that
exact profile headlessly before enabling the timer:

```console
chmod 600 /home/alice/.config/fbn/facebook-auth.json

/home/alice/.local/venvs/fbn/bin/fbn bootstrap \
  --auth-file /home/alice/.config/fbn/facebook-auth.json \
  -i my-group \
  --browser chromium \
  --profile-dir /home/alice/.local/share/fbn/profile
```

After bootstrap succeeds, remove or continue protecting the source file. It is
not needed by the timer. The headed command below is only an optional recovery
path on a trusted workstation:

```console
/home/alice/.local/venvs/fbn/bin/fbn login \
  --browser chromium \
  --profile-dir /home/alice/.local/share/fbn/profile
```

Create `~/.config/systemd/user/fbn-check.service`:

```systemd
[Unit]
Description=Check one Facebook group with fbn
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.config/fbn/fbn.env
ExecStart=/home/alice/.local/venvs/fbn/bin/fbn check --id my-group --browser chromium --headless
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/fbn
```

Create `~/.config/systemd/user/fbn-check.timer`:

```systemd
[Unit]
Description=Run fbn every one to three hours

[Timer]
OnBootSec=10m
OnUnitInactiveSec=1h
RandomizedDelaySec=2h
AccuracySec=5m
Persistent=true
Unit=fbn-check.service

[Install]
WantedBy=timers.target
```

Load and inspect the timer:

```console
systemctl --user daemon-reload
systemctl --user enable --now fbn-check.timer
systemctl --user list-timers fbn-check.timer
journalctl --user -u fbn-check.service
```

Do not place a Facebook password, exported cookies, an authentication-file path,
or the Apprise URL directly in `ExecStart`. If your profile or state paths
differ, update `ReadWritePaths`. A server with no installed browser may need
`python -m playwright install --with-deps chromium` before the timer is enabled.

## Use n8n only as an optional scheduler

`n8n` is not a browser backend or an `fbn` dependency. A self-hosted n8n
schedule may invoke the one-shot command:

```console
fbn check \
  --id my-group \
  --browser chromium \
  --headless
```

Set `FBN_APPRISE_URL`, `FBN_PROFILE_DIR`, and `FBN_STATE_FILE` in the execution
environment, not in the workflow command. Before enabling the workflow,
bootstrap the profile outside n8n with the same headless procedure documented
above. The n8n worker must run locally as the same user, have access to that
profile, and prevent concurrent runs.
Facebook n8n nodes generally use the Graph API; they do not replace `fbn`'s
browser acquisition. Never give the workflow or n8n worker the authentication
export.

## Troubleshooting

### Browser executable not found

Run `fbn doctor` with the same browser options used by `bootstrap`, `login`,
`check`, or `monitor`. Install Playwright Chromium, select an installed
Chrome/Edge channel, or supply an explicit system Chromium path.

### Authentication required

Create a fresh private authentication export and repeat `fbn bootstrap` with
the same group, browser, and profile directory. On a trusted workstation,
headed `fbn login` is an optional recovery path. Never paste authentication
values into the terminal.

### Checkpoint, CAPTCHA, consent, or account action

Automation stops deliberately. Open `fbn login`, resolve the page personally,
then resume the local monitor.

### Profile already in use

Close the other `fbn` or browser process using the dedicated profile. The
profile lock prevents concurrent processes from corrupting browser state.

### Unsupported or changed layout

`fbn` fails closed instead of treating an unrecognized page as an empty group.
Update `fbn` and retry once. Repeated retries do not repair a selector change.

### Blank post page or loading spinner

FBN waits for actual mobile post content within the navigation/scan deadline.
If the deadline expires on a blank shell with no readable text, controls, media,
scroller, or primary post, it records private screenshots and content-free
counters, discards the failed observation, and uses the existing transient
backoff. The first delay is the configured minimum interval; subsequent failures
double it up to 24 hours. The delay survives a restart. There is no immediate
reload or link-reopening loop. Login/account-action prompts, unsupported visible
layouts, and post-identity mismatches remain hard stops.

### No notification on the first check

This is the expected baseline behavior. A later unseen post creates a pending
notification. To test rendering without delivery, use `--dry-run`; dry-run
events remain pending and may appear again.

### An older post appeared in the feed

Facebook can reorder, pin, or later expose a post that `fbn` has not identified
before. After the first check, `fbn` remembers the newest post it has handled.
On later checks, it notifies you about every unsent post above that remembered
post. It will scroll farther than the normal sample when needed to find it.

If the remembered post is not found before the scan limit, `fbn` sends every
visible post it has not sent before, but it does not move the marker. That keeps
the gap open: posts that appear in a later scan are still sent instead of being
mistaken for old posts. Publication dates do not decide whether a notification
is sent.

## Migrating from fbn 0.1

The package name and `fbn` executable remain the same, but the command line now
has subcommands.

| 0.1 | 0.2 |
| --- | --- |
| `fbn --id GROUP ...` | `fbn check --id GROUP ...` or `fbn monitor --id GROUP ...` |
| `--username` / `FBN_FB_USERNAME` | Removed; use one-time `fbn bootstrap` or optional recovery `fbn login` |
| `--password` / `FBN_FB_PASSWORD` | Removed; use one-time `fbn bootstrap` or optional recovery `fbn login` |
| `--cookies-file` | Removed from monitoring; use bounded `fbn bootstrap --auth-file FILE -i GROUP` once |
| `--user-agent` | Removed; the selected browser uses its native user agent |
| `FBN_APPRISE_URL` | Still supported |

The in-memory seen-post set is replaced by SQLite. Expect the first successful
0.2 check to establish a fresh baseline.

## Design and development

The rewrite is documented in:

- [research](https://github.com/viseshrp/fbn/blob/main/docs/RESEARCH.md);
- [specification](https://github.com/viseshrp/fbn/blob/main/docs/SPEC.md);
- [architecture](https://github.com/viseshrp/fbn/blob/main/docs/ARCHITECTURE.md);
- [decision record](https://github.com/viseshrp/fbn/blob/main/docs/ADR.md); and
- [implementation plan](https://github.com/viseshrp/fbn/blob/main/docs/IMPLEMENTATION_PLAN.md).

For a development checkout:

```console
python -m pip install -e '.[dev]'
python -m ruff check .
python -m ruff format --check .
python -m pytest --ignore=tests/test_browser_integration.py
python -m playwright install chromium
python -m pytest tests/test_browser_integration.py
python -m build
python -m twine check dist/*
docker build --tag fbn:local .
```

Automated tests use synthetic local pages and fake notification transports.

## License

The source code is available under the MIT License. See the
[license](https://github.com/viseshrp/fbn/blob/main/LICENSE).
