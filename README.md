# fbn

[![CI](https://github.com/viseshrp/fbn/actions/workflows/ci.yml/badge.svg)](https://github.com/viseshrp/fbn/actions/workflows/ci.yml)
[![Container](https://github.com/viseshrp/fbn/actions/workflows/docker.yml/badge.svg)](https://github.com/viseshrp/fbn/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/github/license/viseshrp/fbn)](https://github.com/viseshrp/fbn/blob/main/LICENSE)

`fbn` watches a Facebook group you belong to and sends you a notification
whenever a new post appears. Notifications are delivered through
[Apprise](https://github.com/caronc/apprise/wiki), so you can send alerts to
email, Telegram, Discord, Ntfy, Pushover, Slack, or 100+ other services.

Instead of relying on brittle raw HTTP requests or asking for your Facebook
password, `fbn` runs a dedicated headless Chromium browser through Playwright,
verifies every post and its canonical link, and stores your session and seen-post
history locally on your own machine.

> **Note:** Version 0.2 is an unreleased research tool for local academic use.

---

## Why run `fbn` with Docker? (Recommended)

Running `fbn` with Docker is the **easiest and most preferred way** to use the
app:

- **Zero browser or Python setup on your host:** Python, Playwright, and the
  matching Chromium browser are already installed inside the container.
- **Works on laptops, servers, and Raspberry Pi:** Official multi-architecture
  images (`linux/amd64` and `linux/arm64`) are published to GitHub Container
  Registry (`ghcr.io/viseshrp/fbn`).
- **One-command management:** Simple `make` commands handle volume creation,
  cookie import, dry-run checks, background monitoring, and live logs.
- **Safe by design:** Runs as a non-root user, mounts your exported cookie file
  only during one-time bootstrap, and keeps your browser profile and post
  history in a persistent Docker volume.

---

## Quick start with Docker (Beginner Guide)

### What you need before starting

1. **Docker** with Docker Compose (`docker compose`) and `make` installed:
   - macOS or Windows: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
   - Linux or Raspberry Pi 4 (64-bit OS): Docker Engine + Docker Compose plugin
2. **A Facebook account** that is already a member of the group you want to
   watch.
3. **A notification destination** supported by
   [Apprise](https://github.com/caronc/apprise/wiki). Common examples:

   | Service | Example `FBN_APPRISE_URL` |
   | --- | --- |
   | Email (Gmail / SMTP app password) | `mailto://user:app-password@gmail.com` |
   | [Ntfy](https://ntfy.sh/) | `ntfy://ntfy.sh/my-private-topic` |
   | Discord webhook | `discord://webhook_id/webhook_token` |
   | Telegram bot | `tgram://bot_token/chat_id` |
   | Pushover | `pover://user_key@token` |

### Step 1: Clone the repository

```console
git clone https://github.com/viseshrp/fbn.git
cd fbn
```

### Step 2: Export your Facebook cookies once

`fbn` never asks for your Facebook username or password. Instead, you export
cookies from a browser where you are already signed in to Facebook:

1. Open Facebook in your normal desktop browser, make sure you are signed in,
   and visit the group you want to monitor so you know your account can view it.
2. Export your browser cookies for `facebook.com` to a file. `fbn`
   automatically accepts any of these common formats:
   - a JSON array from a browser cookie-exporter extension;
   - a Netscape `cookies.txt` file; or
   - a Playwright storage-state JSON file.
3. Save that file in a **private folder outside this repository** (never inside
   the `fbn` git folder or a shared cloud folder) and restrict its permissions:

```console
mkdir -p ~/.config/fbn
chmod 700 ~/.config/fbn
# Move your exported cookie file to ~/.config/fbn/facebook-auth.json, then run:
chmod 600 ~/.config/fbn/facebook-auth.json
```

### Step 3: Create your `.env` configuration file

In the `fbn` repository folder, create a `.env` file with your group ID (or URL
slug), your Apprise notification URL, and the full path to your cookie file:

```dotenv
# The Facebook group slug, numeric ID, or full group URL:
FBN_GROUP=my-group

# Where to send notifications (see the Apprise wiki for 100+ services):
FBN_APPRISE_URL=mailto://user:app-password@example.com

# Absolute path to your private cookie file (keep outside this repo):
FBN_AUTH_FILE=/Users/yourname/.config/fbn/facebook-auth.json

# Optional settings (shown with sensible defaults):
FBN_TIMEZONE=America/New_York
FBN_EVERY=1h
FBN_TO=3h
FBN_DATA_VOLUME=fbn-monitor-data
FBN_IMAGE=ghcr.io/viseshrp/fbn:latest
FBN_UID=1000
FBN_GID=1000
```

> **Linux tip:** Set `FBN_UID` and `FBN_GID` to the output of `id -u` and
> `id -g` on your Linux machine so files inside the persistent volume match your
> user account.

Lock down your `.env` file permissions:

```console
chmod 600 .env
```

### Step 4: Test your setup (Optional dry run)

If you want to verify that your cookies work and see what `fbn` finds before
turning on continuous notifications:

```console
make docker-bootstrap
make docker-check-dry
```

- `make docker-bootstrap` creates the persistent Docker volume if needed,
  imports only `facebook.com` cookies from `FBN_AUTH_FILE`, and confirms that
  the container can open the group feed headlessly.
- `make docker-check-dry` runs one check in `--dry-run` mode and prints the
  summary to your terminal without sending notifications.

### Step 5: Start 24/7 monitoring

Start the background monitor with a single command:

```console
make docker-up
```

`make docker-up` (or simply `make up`) pulls `ghcr.io/viseshrp/fbn:latest` if
needed, creates the `fbn-monitor-data` Docker volume, runs the one-time
`bootstrap` container, and starts the long-running `fbn` monitor container.

Watch live logs at any time:

```console
make docker-logs
```

Check whether the container is running and healthy:

```console
make docker-status
```

Stop the monitor without losing your saved login session or post history:

```console
make docker-down
```

> **What happens on the very first check?**
> The first successful check records a **baseline** of current posts in the
> group so your phone or inbox isn't flooded with old posts. After that first
> baseline check, `fbn` notifies you whenever new posts appear!

---

## Everyday `make` commands

Run `make help` at any time to see all available commands.

### Running & managing `fbn` (Docker)

| Command | Alias | What it does |
| --- | --- | --- |
| `make docker-up` | `make up`, `make docker-run` | Create the data volume if needed, bootstrap cookies, and start the monitor in the background |
| `make docker-down` | `make down` | Stop the monitor container while keeping your persistent profile and post history volume |
| `make docker-logs` | `make logs` | Follow live, secret-free monitor logs (`Ctrl+C` stops viewing logs without stopping the container) |
| `make docker-status` | `make status` | Show container status and health |
| `make docker-bootstrap` | — | Import and validate `FBN_AUTH_FILE` into the persistent Docker volume |
| `make docker-check-dry` | — | Run a one-time check in `--dry-run` mode (prints results without sending notifications) |
| `make docker-check` | — | Run a one-time check and send any pending notifications via `FBN_APPRISE_URL` |
| `make docker-doctor` | — | Run read-only browser and environment diagnostics inside the container |
| `make docker-pull` | — | Pull the latest prebuilt container image (`FBN_IMAGE` or `DOCKER_IMAGE`) from GHCR |
| `make docker-build` | — | Build the container image locally from this checkout (`ghcr.io/viseshrp/fbn:local`) |

### Development, testing & CI commands

| Command | What it does |
| --- | --- |
| `make docker-config` | Validate `compose.yaml`, `docker-compose.yml`, and `deploy/fbn.service` |
| `make docker-smoke` | Build/inspect the container image and run offline smoke, screenshot, and persistence checks |
| `make docker-test` | Run lint, unit tests, package build/Twine/wheel smoke checks, and Playwright browser tests inside a disposable `--network none` container |
| `make docker-test-matrix` | Run unit tests across Python 3.10–3.14 inside disposable `--network none` containers |
| `make docker-push TAGS="..."` | Smoke-test and push a multi-arch (`linux/amd64,linux/arm64`) image to GHCR using `gh` CLI credentials |
| `make install-dev` | Install `fbn` and development dependencies into the active Python environment (used by CI) |
| `make lint` | Run Ruff lint, Ruff format check, and `git diff --check` |
| `make test-unit` | Run unit tests with coverage |
| `make test-browser` | Run sanitized local-page Playwright integration tests |
| `make build` / `make check-dist` / `make smoke-wheel` | Build sdist/wheel, validate with Twine, and smoke-test the built wheel in a clean virtualenv |

---

## Container images on GHCR (`ghcr.io/viseshrp/fbn`)

Multi-architecture images (`linux/amd64` and `linux/arm64`, including 64-bit
Raspberry Pi 4) are automatically built, smoke-tested on native runners, and
published to `ghcr.io/viseshrp/fbn`:

| Tag | Description |
| --- | --- |
| `ghcr.io/viseshrp/fbn:latest` | Latest published image (default in `compose.yaml`) |
| `ghcr.io/viseshrp/fbn:vX.Y.Z` / `ghcr.io/viseshrp/fbn:X.Y.Z` | Version-pinned release images built from git tags / GitHub releases |
| `ghcr.io/viseshrp/fbn:nightly` | Nightly build from `main` (updated daily at `07:23 UTC` and on manual dispatch) |
| `ghcr.io/viseshrp/fbn:nightly-YYYYMMDD-RUN-ATTEMPT` | Immutable timestamped nightly build tag |

To use the nightly image or pin a specific version, set `FBN_IMAGE` in your
`.env` file:

```dotenv
FBN_IMAGE=ghcr.io/viseshrp/fbn:nightly
```

Then pull and restart:

```console
make docker-pull
make docker-up
```

If you prefer to build the image locally instead of pulling from GHCR:

```console
make docker-build DOCKER_IMAGE=ghcr.io/viseshrp/fbn:local
FBN_IMAGE=ghcr.io/viseshrp/fbn:local make docker-up
```

### How the Docker Compose setup works

The included image is based on Ubuntu 24.04, installs Playwright 1.61.0 and its
matching managed Chromium build, and runs `fbn` as a non-root user (`fbn:fbn`).
The default image command is `fbn --help`; starting the bare image never
contacts Facebook.

When you run `make docker-up` (or `docker compose up --detach`):

1. **One-shot `bootstrap` runs first:** It mounts your cookie export read-only
   at `/run/secrets/facebook_auth`, validates access to the group headlessly,
   and updates the persistent browser profile inside the `fbn-monitor-data`
   volume (`/home/fbn/.local/share/fbn`).
2. **Long-running `fbn` monitor starts only after bootstrap succeeds:** The
   monitor service never mounts your raw cookie export file. It runs
   `fbn monitor --browser chromium --headless --verbose --include-errors ...`
   with a 1 GiB memory limit, swap disabled, a 1 GiB `/dev/shm` allocation for
   Chromium, and `init: true` for clean signal forwarding.
3. **Fail-closed restart policy (`restart: "no"`):** The internal scheduler
   automatically backs off on temporary network or loading glitches. If
   Facebook asks for re-authentication, a security checkpoint, or changes its
   page layout, the container stops cleanly and sends one secret-free alert
   (because `--include-errors` is enabled) instead of looping forever.
4. **Passive `HEALTHCHECK`:** Docker runs `python -m fbn.health`, which checks
   a container-local scheduler lease file. It never launches a browser, opens
   your profile, or contacts Facebook.

### Supervise Docker Compose with `systemd` (Optional for Linux servers / Raspberry Pi)

After bootstrapping your profile once, you can use `deploy/fbn.service` to
supervise the Compose monitor on a Linux server or Raspberry Pi (assuming a
checkout at `/opt/fbn-compose`):

```console
sudo install -m 644 deploy/fbn.service /etc/systemd/system/fbn.service
sudo systemctl daemon-reload
sudo systemctl enable --now fbn.service
systemctl status fbn.service
```

The systemd unit runs Compose in the foreground with `--no-deps` (so reboots
reuse your already-authenticated volume without re-importing the cookie file)
and propagates the monitor's exit code. Unexpected process crashes restart after
15 minutes (up to 3 starts per hour), while known authentication, checkpoint,
access, layout, or configuration exit codes stay stopped via
`RestartPreventExitStatus`.

---

## Alternative: Install and run locally with Python (Without Docker)

Docker is the recommended way to run `fbn`, but you can also run it directly in
a Python 3.10+ virtual environment on macOS, Linux, or Windows.

### 1. Install `fbn` and a browser

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m playwright install chromium
fbn doctor --browser chromium
```

On a minimal Linux server, install Chromium along with its system libraries:

```console
python -m playwright install --with-deps chromium
```

You can also use an existing Google Chrome (`--browser chrome`), Microsoft Edge
(`--browser msedge`), or system Chromium binary
(`--browser executable --executable-path /usr/bin/chromium`). `fbn doctor` is
read-only and checks browser and path readiness without contacting Facebook.

### 2. Bootstrap authentication once

```console
chmod 600 /private/path/facebook-auth.json

fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chromium
```

By default, the dedicated browser profile and SQLite state file are stored in
your operating system's private per-user application-data directory. You can
override their locations with environment variables or CLI flags:

```console
export FBN_PROFILE_DIR=/private/path/to/fbn/profile
export FBN_STATE_FILE=/private/path/to/fbn/state.sqlite3
```

### 3. Run a single check or monitor continuously

Start with a dry run:

```console
fbn check \
  --id my-group \
  --browser chromium \
  --dry-run
```

Run a real check with notifications:

```console
export FBN_APPRISE_URL='mailto://user:app-password@example.com'

fbn check \
  --id my-group \
  --browser chromium
```

Or monitor continuously on a randomized interval (defaults to 1–3 hours; minimum
15 minutes, maximum 365 days, units `s`, `m`, `h`, `d`, `w`):

```console
fbn monitor \
  --id my-group \
  --browser chromium \
  --timezone America/New_York \
  --every 1h \
  --to 3h
```

Useful options for `check` and `monitor`:

- `--sample-count`: normal number of recent posts checked per scan (default `10`).
- `--timezone`: IANA timezone used to render and parse Facebook timestamps
  (default `UTC`).
- `--notify-initial`: send notifications for the first visible sample instead of
  recording a silent baseline.
- `--include-errors`: send a concise, secret-free notification if `monitor`
  stops on a hard error.
- `-v` / `--verbose`: emit readable, secret-free timestamped lifecycle logs.

### Optional: Run local checks with a `systemd` user timer or `n8n`

<details>
<summary><strong>Click to expand: systemd user timer setup</strong></summary>

A `systemd` user timer that runs `fbn check` periodically is a lightweight way
to schedule bounded runs on a native Linux host. Bootstrap the profile first as
the same Unix user.

Create `~/.config/fbn/fbn.env` (`chmod 600 ~/.config/fbn/fbn.env`):

```dotenv
FBN_APPRISE_URL=mailto://user:app-password@example.com
FBN_PROFILE_DIR=/home/alice/.local/share/fbn/profile
FBN_STATE_FILE=/home/alice/.local/share/fbn/state.sqlite3
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

Enable and inspect the timer:

```console
systemctl --user daemon-reload
systemctl --user enable --now fbn-check.timer
systemctl --user list-timers fbn-check.timer
journalctl --user -u fbn-check.service
```

</details>

<details>
<summary><strong>Click to expand: n8n external scheduler setup</strong></summary>

`n8n` is not a browser backend or an `fbn` dependency, but a self-hosted `n8n`
schedule can invoke the one-shot command:

```console
fbn check \
  --id my-group \
  --browser chromium \
  --headless
```

Set `FBN_APPRISE_URL`, `FBN_PROFILE_DIR`, and `FBN_STATE_FILE` in the worker's
execution environment, not in the workflow command. Bootstrap the profile
outside `n8n` first, ensure the worker runs as the same user, and prevent
overlapping runs. Never pass your cookie export file into `n8n`.

</details>

---

## Troubleshooting & FAQ

### Why didn't I get a notification on the first check?

This is intentional! The first non-empty check records a **baseline** so you
aren't spammed with existing posts. Any new post that appears after the baseline
will trigger a notification. If you want to preview output without marking posts
as delivered, run `make docker-check-dry` (or `fbn check --dry-run`). If you
truly want notifications for the initial batch, pass `--notify-initial`.

### `AuthenticationRequiredError` — "not signed in"

Your Facebook session cookies have expired or could not access the group.
Export fresh `facebook.com` cookies from your browser, save them to your
private `FBN_AUTH_FILE`, and run `make docker-bootstrap` (or `make docker-up`).
On a workstation with a display, native users can also run `fbn login` for
interactive recovery.

### `AccountActionRequiredError` — Checkpoint, CAPTCHA, or consent screen

Automation stops immediately so it never triggers repeated security challenges.
Sign in to Facebook in your personal browser (or via `fbn login` on a trusted
desktop), resolve the prompt, export fresh cookies, and run
`make docker-bootstrap`.

### `ProfileInUseError` — "Profile already in use"

Only one `fbn` browser process may use a profile directory at a time so browser
state is never corrupted. Stop any other running `fbn` container (`make docker-down`)
or local process before starting a new one.

### `BrowserUnavailableError` — Browser executable not found

In Docker, Chromium is pre-installed; run `make docker-doctor` to verify. For a
native Python install, run `python -m playwright install chromium` and verify
with `fbn doctor --browser chromium`.

### `LayoutChangedError` — Unsupported or changed layout

`fbn` fails closed rather than guessing or treating an unrecognized page as an
empty group. Update to the latest image (`make docker-pull && make docker-up`)
and inspect `make docker-logs`.

When a feed/layout or post-link check fails, `fbn` automatically saves private
viewport screenshots (`feed.png`, optional `post.png`) and content-free
structural diagnostics (`metadata.json`, `feed-layout.json`, `post-layout.json`)
under `failure-screenshots/` inside the data directory (in Docker:
`/home/fbn/.local/share/fbn/failure-screenshots/` inside `fbn-monitor-data`).
Only the latest 10 failure folders are kept, with inputs masked and owner-only
permissions (`0700`/`0600`).

### Blank post page or loading spinner

`fbn` waits for actual mobile post content within the navigation/scan deadline.
If a page remains a blank loading shell with no readable text, controls, media,
scroller, or primary post, `fbn` captures private failure diagnostics, discards
the incomplete scan without losing state, and retries up to 3 times within the
same check (after 5, 10, and 20 seconds) if schedule headroom permits, or backs
off until the next scheduled slot.

### Why did an older post appear in a notification?

Facebook can reorder, pin, or later expose a post that `fbn` had not seen
before. After the first check, `fbn` remembers the newest post it handled (the
notification boundary marker) and scrolls to find it on subsequent checks,
notifying you about any unsent post above that marker. If the marker isn't
reached before the scan limit, `fbn` sends visible unsent posts while keeping
the old marker in place so later scans can finish catching up.

---

## How `fbn` works under the hood (Technical Reference)

### Mobile reader and post verification

`check`, `monitor`, `bootstrap`, and `login` use the mobile Facebook layout at
`m.facebook.com` with Playwright's standard Pixel 7 emulation:

- **Explicit chronological sorting:** Selects and verifies **New posts** before
  reading cards.
- **Strict primary-post boundary:** Reads each main post's authored body and
  ignores quoted/embedded posts, comments, replies, and comment previews. The
  post's first action row forms the outer content boundary. Within that
  boundary, `fbn` partitions authored message blocks, attachments (photo/video
  buttons, media credit cards, album overlays, rich image previews, event
  cards), recognized interface panels, and discussion. Unknown interactive
  controls or unclaimed text fail closed.
- **Truncated post expansion:** For shortened posts (`See more`), `fbn` records
  the visible text before and after the expander, expands the message, and
  verifies that both sides are preserved.
- **Canonical link verification:** Every post link is copied from that main
  post's own **Copy link** menu and opened in a fresh tab (closed immediately
  after verification). The destination page must match the author, full body,
  publication time, group identity, and a permanent canonical post ID.
- **Bounded catch-up scanning:** Later checks scroll toward the saved
  notification marker and read at least one additional pass after finding it,
  bounded by `--max-scrolls`, the catch-up sample limit, and a shared 90-second
  scan budget.

### Durable state and schema migrations

State is stored in SQLite (WAL mode) and committed before notification delivery
so pending notifications survive restarts (at-least-once delivery):

- **Schema version 4** upgrades saved direct links to canonical post IDs, merges
  seen history and outbox receipts atomically, and archives unlinked legacy
  records in `identity_archive`.
- **Schema version 5** repairs an archived, unreachable delivered marker once
  using the latest later canonical delivery receipt while keeping the archive
  intact.

### Migrating from `fbn` 0.1

| `fbn` 0.1 | `fbn` 0.2 |
| --- | --- |
| `fbn --id GROUP ...` | `make docker-up` (Docker) or `fbn check` / `fbn monitor` |
| `--username` / `FBN_FB_USERNAME` | Removed; use one-time `bootstrap` with a cookie export |
| `--password` / `FBN_FB_PASSWORD` | Removed; use one-time `bootstrap` with a cookie export |
| `--cookies-file` | Replaced by one-time `bootstrap --auth-file FILE -i GROUP` |
| `--user-agent` | Removed; the selected browser uses its native user agent |
| `FBN_APPRISE_URL` | Still supported |

---

## Design documents & contributor testing

Architecture and design decisions are documented in:

- [Research](https://github.com/viseshrp/fbn/blob/main/docs/RESEARCH.md)
- [Specification](https://github.com/viseshrp/fbn/blob/main/docs/SPEC.md)
- [Architecture](https://github.com/viseshrp/fbn/blob/main/docs/ARCHITECTURE.md)
- [Architecture Decision Record (ADR)](https://github.com/viseshrp/fbn/blob/main/docs/ADR.md)
- [Implementation Plan](https://github.com/viseshrp/fbn/blob/main/docs/IMPLEMENTATION_PLAN.md)

To validate a development checkout inside isolated Docker containers (matching CI
without installing test dependencies on the host):

```console
make docker-config
make docker-build
make docker-smoke
make docker-test
make docker-test-matrix
```

All automated tests run offline against synthetic local HTML fixtures and fake
notification transports; they never contact Facebook or send real notifications.

## License

The source code is available under the MIT License. See
[LICENSE](https://github.com/viseshrp/fbn/blob/main/LICENSE).
