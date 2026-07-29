# fbn 0.2 specification

## Purpose

`fbn` monitors the recent visible posts in one Facebook group and sends a
notification when it observes a new post. It is a local, pip-installable command
line tool for unreleased, local academic research. It must run unattended after
setup on Ubuntu ARM64, including a Raspberry Pi 4, and in an Ubuntu-based
container on either `linux/amd64` or `linux/arm64`.

The 0.2 rewrite replaces direct HTTP/mobile-page scraping and repeated
credential submission with a persistent browser profile bootstrapped from an
existing authenticated session.

## User flows

### Install and authenticate

```console
python -m pip install .
python -m playwright install chromium
chmod 600 /private/path/facebook-auth.json
fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chromium
```

`fbn bootstrap` is fully headless and noninteractive. It imports Facebook-domain
cookies from an explicit Playwright storage-state JSON object, exported-cookie
JSON array, or Netscape `cookies.txt`, then proves authentication and access by
opening the requested group. It never asks for a Facebook password or accepts
cookie values through arguments or environment variables.

An installed stable Chrome channel remains an optional desktop alternative:

```console
fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chrome
```

On Ubuntu ARM64 and Raspberry Pi 4, the supported server runtime is
Playwright-managed regular Chromium:

```console
python -m pip install .
python -m playwright install --with-deps chromium
fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chromium
fbn monitor --id my-group --browser chromium --headless
```

Bootstrap and monitoring use the same dedicated profile path. No display, X11,
VNC, credential automation, uploaded profile, or exposed browser-debugging
endpoint is required. The headed `fbn login` command is an optional recovery
path only when the user must personally resolve an account action.

### Perform one bounded check

```console
fbn check \
  --id my-group \
  --browser chromium \
  --apprise-url 'json://localhost:8000/notify'
```

The first successful check establishes a baseline. Later checks notify only for
post IDs that are not already in the durable state store.

### Run continuously

```console
fbn monitor \
  --id my-group \
  --browser chromium \
  --every 1h \
  --to 3h \
  --apprise-url 'mailto://user:token@example.com'
```

`monitor` sleeps for a random duration in the inclusive range after each check.
It handles SIGINT/SIGTERM and closes the browser before exiting.

### Deploy with Docker

The supported image is based on Ubuntu 24.04 and must build for both
`linux/amd64` and `linux/arm64`. It installs the Playwright-managed Chromium
binary and Linux dependencies with:

```console
python -m playwright install --with-deps chromium
```

The long-running container runs `fbn monitor --browser chromium --headless` as a
non-root user. A persistent volume holds both the dedicated profile and SQLite
state. A profile-gated one-shot Compose service runs `fbn bootstrap` against
that same volume. It receives the host authentication file as a read-only
`/run/secrets/facebook_auth` mount. The monitor service never receives that
file.

Notification credentials and other secrets are supplied only when the container
runs; authentication material is never an environment variable, build argument,
build-context file, or image layer. The container health check imports only the
installed `fbn` package. It never runs a Facebook check, launches a browser,
navigates to Facebook, or opens the authenticated profile.

### Diagnose an installation

```console
fbn doctor --browser chromium
```

`doctor` reports the package version, profile/state paths, browser availability,
and safe next steps. It never prints cookies, notification secrets, or page
content.

## Functional requirements

### FR-1: bounded persistent authentication bootstrap

- A browser profile is stored in the platform user-data directory by default.
- The profile path can be overridden with `--profile-dir` or
  `FBN_PROFILE_DIR`.
- The profile directory is created with owner-only permissions on Unix.
- `fbn bootstrap --auth-file PATH -i GROUP` is fully headless and
  noninteractive.
- Supported inputs are a Playwright storage-state JSON object with a `cookies`
  array, an exported-cookie JSON array, and Netscape `cookies.txt`.
- The source file is bounded to 10 MiB and decoded as UTF-8. Cookie names and
  values never appear in parser, browser, or CLI output.
- Only `facebook.com` and subdomain cookies are imported. Non-Facebook cookies
  are ignored, storage-state origins are ignored, and an input with no
  Facebook-domain cookies is rejected.
- Bootstrap validity does not depend on undocumented cookie names. The selected
  browser must load the requested group and classify an authenticated,
  accessible group feed.
- A bootstrap failure restores the profile's previous cookies; a success leaves
  the imported session in the persistent profile. The source file is not
  modified or copied wholesale into the profile.
- Bootstrap and later monitoring use the same dedicated profile, including when
  it is stored in a container volume.
- A private compatibility marker binds the profile to its initializing browser
  selection. A conflicting browser or executable path is rejected rather than
  risking profile corruption.
- The headed `fbn login` flow remains an optional recovery command and uses the
  same profile. It is not required for initial server or container bootstrap.
- Username/password options, cookie values in command/environment data, and
  per-monitor cookie-file options are removed.

### FR-2: bounded browser acquisition

- Supported browser choices are `chrome`, `chromium`, `msedge`, and
  `executable`.
- Ubuntu ARM64 and Raspberry Pi 4 use Playwright-managed Chromium installed with
  `python -m playwright install --with-deps chromium`.
- `--browser chromium` selects regular Chromium with Playwright
  `channel="chromium"` so headless bootstrap and monitoring use the same browser
  family instead of the separate headless-shell executable.
- `--executable-path` remains an explicit fallback for a compatible
  administrator-managed system browser.
- Chromium and headless operation are the defaults for bootstrap, checks, and
  monitoring.
- `--headed` is an explicit check/monitor troubleshooting opt-in on a trusted
  display.
- Unattended service definitions retain explicit `--headless` for operational
  clarity even though it is the default.
- The browser uses its native user agent and fingerprint. `fbn` does not spoof
  either.
- A check limits navigation time, number of extracted posts, number of scrolls,
  and consecutive stagnant scrolls.
- A sample count must be between 1 and 50.
- `--timezone` accepts an IANA timezone name and defaults to `UTC`. It controls
  browser rendering, timestamp interpretation, and the same-day notification
  boundary.

### FR-3: page-state classification

Before interpreting the feed, `fbn` classifies the page as one of:

- authenticated group feed;
- authentication required or expired;
- checkpoint, consent, CAPTCHA, or account action required;
- access denied or group unavailable;
- transient navigation/rate-limit failure; or
- unsupported/changed layout.

Authentication and account-action states are hard stops. They are not retried in
the same run.

### FR-4: post extraction

- Identity comes from canonical `/groups/<group>/posts/<post>`,
  `/groups/<group>/permalink/<post>`, or group-photo
  `photo/?set=gm.<post>&idorvanity=<group>` links.
- Tracking query parameters are removed from stored URLs.
- Each post contains a stable ID, canonical URL, normalized visible text, an
  optional author, and a parsed publication time when Facebook's rendered
  timestamp is recognized.
- Allowlisted English Facebook timestamp forms are parsed with `dateparser`
  using the timezone-aware scan time as an explicit relative base.
- Facebook's timestamp character decoys are excluded by retaining only glyphs
  rendered inside the timestamp link's visible rectangle.
- Text is capped to prevent unbounded notification/state size.
- Generated class names are not part of the primary selector contract.
- Results are deterministically ordered by their visible feed order.
- If a numeric group URL renders post permalinks under a custom group alias,
  `fbn` accepts that alias only when exactly one candidate is exposed by the
  visible group-navigation tablist. Arbitrary links elsewhere in the page or
  feed never expand the accepted group identity.
- A zero-post page is never treated as an empty group until the page state has
  been classified.

### FR-5: durable state and delivery

- SQLite stores group initialization, seen post IDs, and pending notification
  records.
- The state path can be overridden with `--state-file` or `FBN_STATE_FILE`.
- On Unix, a newly created state parent uses mode `0700`; an existing parent
  keeps its current mode. The database and its WAL/SHM sidecars use `0600`.
- The first non-empty successful scan is a baseline unless `--notify-initial`
  is explicitly supplied.
- New posts and their outbox entries are inserted atomically.
- Every unseen supported post is stored for deduplication. An initialized group
  creates an outbox entry only when the post has a recognized Facebook
  publication date equal to the current calendar date in `--timezone`.
- The eligibility rule is a calendar comparison, not an elapsed-age comparison.
  A post immediately before midnight is ineligible immediately after midnight,
  while a post from early today remains eligible late today.
- Missing, malformed, materially future-skewed, or other-day publication
  timestamps fail closed: the post is marked seen but is not notified.
- Failed notification entries remain pending across process restarts.
- Delivery is at-least-once: a process crash after a send but before its commit
  may cause a duplicate, but it must not silently lose a pending post.
- Post body/author data are cleared from the outbox after successful delivery;
  stable IDs and canonical links remain for deduplication.

### FR-6: notification

- Apprise remains the notification transport.
- `--apprise-url` and `FBN_APPRISE_URL` are supported.
- A failed `Apprise.add()` or `Apprise.notify()` result is an error.
- Notifications contain the group key, author when available, normalized text,
  and canonical post URL.
- Notification bodies are plain text. Facebook content is not interpolated into
  raw HTML.
- `--dry-run` prints the would-be notification and does not require an Apprise
  URL.
- `--include-errors` may notify a concise error category, but never a cookie,
  browser profile value, page dump, or Apprise URL.

### FR-7: scheduling

- Durations use a positive integer followed by `s`, `m`, `h`, `d`, or `w`.
- Parsing uses a full-string match.
- The default interval range is 1–3 hours.
- `--to` requires `--every`, must be greater than or equal to it, and may use a
  different unit after normalization.
- Intervals shorter than 15 minutes are rejected.
- Intervals longer than 365 days are rejected.
- Transient failures use bounded exponential backoff with a maximum of 24 hours.
- Configuration, auth, account-action, browser-profile, and layout failures exit
  nonzero instead of looping.

### FR-8: CLI and exit behavior

- Commands are `bootstrap`, `login`, `check`, `monitor`, and `doctor`.
- The console entry point remains `fbn`; `python -m fbn` remains supported.
- `-V/--version` prints the version. `-v/--verbose` controls logging.
- Exceptions are mapped to concise Click errors and stable nonzero exit codes.
- Authentication values are accepted only through the file named by
  `--auth-file`, never as positional arguments or environment values. Secrets
  are not emitted in logs.

### FR-9: Ubuntu ARM64 and container deployment

- Ubuntu 22.04, 24.04, or 26.04 ARM64 is an eligible native runtime; Ubuntu
  24.04 ARM64 is the deployment baseline for Raspberry Pi 4 and Docker.
- A Raspberry Pi 4 deployment requires a 64-bit Ubuntu installation and the
  Playwright-managed regular Chromium path described in FR-2.
- The project provides an Ubuntu 24.04 container definition that can build for
  `linux/amd64` and `linux/arm64`.
- The runtime container is non-root.
- Browser profile and SQLite state paths are mounted on one persistent,
  owner-controlled volume and survive container replacement.
- Facebook authentication is created by a fully headless, one-shot
  `fbn bootstrap` service against that volume. The service imports a host file
  only through a read-only Compose secret at
  `/run/secrets/facebook_auth`.
- The bootstrap service is behind the explicit `bootstrap` Compose profile. The
  default long-running monitor has no mount or reference to the authentication
  source file.
- Passwords, authentication values in environment variables, uploaded browser
  profiles, and remote-debugging endpoints are not supported.
- The supported Compose workload is a headless `fbn monitor` using
  `--browser chromium`.
- The monitor service restart policy is `no`. Transient retry/backoff belongs to
  the internal scheduler; authentication, account-action, access, profile, and
  layout hard-stop exits remain stopped for operator action.
- Authentication and Apprise secrets are injected only at runtime and are
  absent from image build arguments, layers, and image metadata. The
  documented authentication source lives outside the build context.
- The health check is exactly a local `python -c 'import fbn'` package probe. It
  must not invoke `check` or `monitor`, launch a browser, navigate to Facebook,
  inspect the authenticated profile, or infer account/group health.

## Local security requirements

- Browser challenges are hard stops. A user may provide a fresh authenticated
  export or personally resolve the action with optional headed `fbn login`.
- The tool does not upload browser profiles, cookies, traces, screenshots, or
  extracted group content.
- Container deployment does not expose its authentication secret, persistent
  volume, display, VNC, or browser-debugging endpoint to an untrusted network.
- Live Facebook credentials and content are forbidden in tests and CI.

## Non-goals

- Bulk group scraping, historical export, comments, reactions, member lists, or
  media downloads.
- Meta Graph API integration.
- Automated username/password login, 2FA, consent, CAPTCHA, or checkpoint
  handling.
- Running browser-use, Browserless, Apify, or n8n as a runtime dependency.
- Exact-once delivery across an arbitrary notifier/network crash boundary.
- Claiming complete monitoring when Facebook's feed ordering or virtualized DOM
  omits a post.

## Compatibility and migration

- Package name and executable remain `fbn`.
- `--id`, `--sample-count`, `--every`, `--to`, `--apprise-url`,
  `FBN_APPRISE_URL`, `--include-errors`, and `--verbose` remain, now on the
  `check`/`monitor` commands.
- `--username`, `--password`, `--cookies-file`, and `--user-agent` are removed.
  Users migrate authentication to the bounded, one-time
  `fbn bootstrap --auth-file PATH -i GROUP` flow. `fbn login` is an optional
  recovery path.
- Python support becomes 3.10+.
- Browser binaries are an explicit post-install step; pip installation remains
  standard and does not silently download a browser.
- Ubuntu ARM64 and container users install Playwright-managed Chromium and its
  operating-system dependencies explicitly with
  `python -m playwright install --with-deps chromium`.

## Acceptance criteria

The rewrite is complete when:

1. a wheel builds and installs in a clean virtual environment;
2. `fbn --help`, `fbn --version`, and `python -m fbn --help` succeed;
3. sanitized local fixtures cover feed, login, checkpoint, blocked,
   access-denied, empty, and layout-changed states;
4. unit tests prove baseline, restart-safe deduplication, durable pending
   delivery, deterministic ordering, strict schedule validation, redaction, and
   nonzero failure exits;
5. unit tests cover all three authentication-file formats, Facebook-domain
   filtering, storage-origin exclusion, size and shape validation, secret-free
   errors, group-access validation, and rollback on bootstrap failure;
6. no runtime dependency on `facebook-scraper`, `schedule`, Tenacity,
   or browser-use remains;
7. no test contacts Facebook;
8. Ruff, pytest, build, Twine validation, clean-wheel smoke tests, and
   `git diff --check` pass;
9. CI runs on pushes and pull requests, with no package-publication workflow;
10. a sanitized local-page integration test launches the actual
   Playwright-managed regular Chromium browser headlessly on native Ubuntu
   ARM64, including a Raspberry Pi 4 hardware target, and does not contact
   Facebook;
11. the Ubuntu 24.04 container configuration validates and the image builds for
    both `linux/amd64` and `linux/arm64`;
12. container smoke tests prove that the process is non-root, the profile/state
    volume persists across replacement, runtime configuration is not baked into
    the image, and the health check performs no Facebook navigation; and
13. the one-shot headless bootstrap receives its source through
    `/run/secrets`, the monitor service has no source-file mount, and a
    bootstrap followed by an unattended monitor is manually verified against
    the same persistent profile on the Raspberry Pi and Docker deployment paths.
