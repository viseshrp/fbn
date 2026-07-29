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
exit code. Only transient navigation failures are backed off and retried.

Useful options include:

- `--sample-count`: cap the number of recent posts inspected;
- `--timezone`: set the IANA timezone used to interpret Facebook timestamps and
  decide whether a post was published today; the default is `UTC`;
- `--notify-initial`: notify for the first visible sample instead of baselining;
- `--include-errors`: notify a concise, redacted operational error; and
- `-v` / `--verbose`: emit secret-free lifecycle and browser diagnostics as
  readable timestamped lines to standard output, without page or cookie dumps.

Use `fbn COMMAND --help` for the complete command-specific options.

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
FBN_TIMEZONE=America/New_York
FBN_EVERY=1h
FBN_TO=3h
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

Compose refuses to start the monitor unless the group and Apprise URL are
present. Cookies, browser state, and group content are not baked into the
image. Docker does retain container environment metadata, including the
Apprise URL, so only users trusted with the Docker daemon should be able to
inspect the deployment.

### Bootstrap the persistent profile once

The named `fbn-data` volume is mounted at
`/home/fbn/.local/share/fbn`. It holds both the dedicated browser profile and
SQLite state. The separate `bootstrap` service mounts the authentication file
as a read-only Compose secret, validates group access headlessly, and writes the
resulting session only to that volume:

```console
chmod 600 /absolute/private/path/facebook-auth.json

FBN_AUTH_FILE=/absolute/private/path/facebook-auth.json \
  docker compose --profile bootstrap run --rm --no-deps bootstrap
```

Inside the one-shot container the source is available only at
`/run/secrets/facebook_auth`. Its contents are not placed in an environment
variable, copied into the image, or mounted into the long-running `fbn`
service. The `bootstrap` service gets the group from `FBN_GROUP` in `.env`; its
command is equivalent to:

```console
fbn bootstrap \
  --auth-file /run/secrets/facebook_auth \
  -i "$FBN_GROUP" \
  --browser chromium
```

The command performs no prompts and needs no display, X11, VNC, or browser
debugging port. When it succeeds, remove or protect the source authentication
file according to your recovery policy. To replace an expired session, stop the
monitor, repeat the one-shot bootstrap with a fresh export, and restart it.

### Run the monitor

After bootstrap, start only the default monitor service:

```console
docker compose up --detach fbn
docker compose logs --follow fbn
```

The Compose command is explicitly
`fbn monitor --browser chromium --headless --verbose ...`. Chromium uses a 1
GiB shared memory allocation, and `init: true` forwards termination cleanly.
The monitor writes human-readable Loguru records to standard output, so the
`docker compose logs --follow fbn` command above shows startup, waits, scan
counts, delivery counts, and retry categories as they happen. Each line has a
UTC timestamp, level, component, plain-language event, and safe `key=value`
context. Non-interactive container output contains no ANSI color codes. Records
never include page text, cookie values, authentication-file paths, or Apprise
URLs. The profile-gated `bootstrap` service does not start with this command,
and the monitor has no authentication-file secret mount. Both services allow
two minutes for graceful termination so bootstrap has time to verify cookie
rollback and the monitor can close Chromium.

The monitor restart policy is deliberately `no`. Its internal scheduler already
backs off transient navigation failures; authentication, checkpoint, access,
profile, and layout hard stops must leave the container stopped instead of
causing a rapid Docker restart loop. Inspect the logs, resolve the condition,
and run `docker compose up --detach fbn` again. If startup after a host reboot
is required, configure it explicitly without an automatic failure restart.
Stop the container without deleting its profile/state volume:

```console
docker compose down
```

Do not add `--volumes` unless permanently deleting the authenticated profile and
deduplication history is intentional. The image `HEALTHCHECK` runs only
`python -c 'import fbn'`. It does not launch a browser, open the profile, or
navigate to Facebook. This proves only that the installed package remains
importable inside the container; it does not prove session, account, group, or
monitor health.

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

### No notification on the first check

This is the expected baseline behavior. A later unseen post creates a pending
notification. To test rendering without delivery, use `--dry-run`; dry-run
events remain pending and may appear again.

### An older post appeared in the feed

Facebook can reorder, pin, or later expose a post that `fbn` has not identified
before. The post is recorded as seen, but it is notified only when Facebook's
rendered publication date is today in `--timezone`. This is a calendar-day
boundary, not a rolling 24-hour window: a post from 11:59 PM yesterday is
ineligible after midnight, while a post from early this morning remains
eligible late tonight. Unknown timestamp layouts fail closed.

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
