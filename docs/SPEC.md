# fbn 0.2 specification

## Purpose

`fbn` monitors the recent visible posts in one Facebook group and sends a
notification when it observes a new post. It is a local, pip-installable command
line tool for a user who already has legitimate access to the group.

The 0.2 rewrite replaces direct HTTP/mobile-page scraping and repeated
credential submission with a persistent, user-authenticated browser profile.

## User flows

### Install and authenticate

```console
python -m pip install fbn
python -m playwright install chromium
fbn login --browser chromium
```

`fbn login` opens a visible browser using a dedicated profile. The user completes
login, 2FA, consent, and any checkpoint personally. `fbn` never asks for the
Facebook password or raw cookie values.

An installed stable Chrome channel is the recommended desktop browser:

```console
fbn login --browser chrome
```

### Perform one bounded check

```console
fbn check \
  --id my-group \
  --browser chrome \
  --apprise-url 'json://localhost:8000/notify' \
  --acknowledge-automation-risk
```

The first successful check establishes a baseline. Later checks notify only for
post IDs that are not already in the durable state store.

### Run continuously

```console
fbn monitor \
  --id my-group \
  --every 1h \
  --to 3h \
  --apprise-url 'mailto://user:token@example.com' \
  --acknowledge-automation-risk
```

`monitor` sleeps for a random duration in the inclusive range after each check.
It handles SIGINT/SIGTERM and closes the browser before exiting.

### Diagnose an installation

```console
fbn doctor --browser chromium
```

`doctor` reports the package version, profile/state paths, browser availability,
and safe next steps. It never prints cookies, notification secrets, or page
content.

## Functional requirements

### FR-1: persistent local authentication

- A browser profile is stored in the platform user-data directory by default.
- The profile path can be overridden with `--profile-dir` or
  `FBN_PROFILE_DIR`.
- The profile directory is created with owner-only permissions on Unix.
- Login is interactive and always headed.
- Username/password and raw-cookie command options are removed.

### FR-2: bounded browser acquisition

- Supported browser choices are `chrome`, `chromium`, `msedge`, and
  `executable`.
- `--executable-path` supports system Chromium deployments such as a Raspberry
  Pi.
- Monitoring is headed by default. `--headless` is an explicit opt-in.
- The browser uses its native user agent and fingerprint. `fbn` does not spoof
  either.
- A check limits navigation time, number of extracted posts, number of scrolls,
  and consecutive stagnant scrolls.
- A sample count must be between 1 and 50.

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

- Identity comes from canonical `/groups/<group>/posts/<post>` or
  `/groups/<group>/permalink/<post>` links.
- Tracking query parameters are removed from stored URLs.
- Each post contains a stable ID, canonical URL, normalized visible text, and an
  optional author.
- Text is capped to prevent unbounded notification/state size.
- Generated class names are not part of the primary selector contract.
- Results are deterministically ordered by their visible feed order.
- A zero-post page is never treated as an empty group until the page state has
  been classified.

### FR-5: durable state and delivery

- SQLite stores group initialization, seen post IDs, and pending notification
  records.
- The state path can be overridden with `--state-file` or `FBN_STATE_FILE`.
- The first non-empty successful scan is a baseline unless `--notify-initial`
  is explicitly supplied.
- New posts and their outbox entries are inserted atomically.
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
- Transient failures use bounded exponential backoff with a maximum of 24 hours.
- Configuration, auth, account-action, browser-profile, and layout failures exit
  nonzero instead of looping.

### FR-8: CLI and exit behavior

- Commands are `login`, `check`, `monitor`, and `doctor`.
- The console entry point remains `fbn`; `python -m fbn` remains supported.
- `-V/--version` prints the version. `-v/--verbose` controls logging.
- Exceptions are mapped to concise Click errors and stable nonzero exit codes.
- Secrets are not accepted as positional arguments and are not emitted in logs.

## Compliance and safety requirements

- The CLI and README must state that Meta may prohibit automated collection
  without prior permission even for a logged-in account.
- `check` and `monitor` require `--acknowledge-automation-risk`.
- No CAPTCHA solving, proxy rotation, fingerprint spoofing, webdriver hiding,
  stealth browser fork, private GraphQL replay, or undocumented API call is
  included.
- Browser challenges are handed back to the user through `fbn login`.
- The tool does not upload browser profiles, cookies, traces, screenshots, or
  extracted group content.
- Live Facebook credentials and content are forbidden in tests and CI.

## Non-goals

- Guaranteed undetectability or avoidance of account action.
- Bulk group scraping, historical export, comments, reactions, member lists, or
  media downloads.
- Meta Graph API integration.
- Automated Facebook login, 2FA, consent, CAPTCHA, or checkpoint handling.
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
  Users migrate with `fbn login`.
- Python support becomes 3.10+.
- Browser binaries are an explicit post-install step; pip installation remains
  standard and does not silently download a browser.

## Acceptance criteria

The rewrite is complete when:

1. a wheel builds and installs in a clean virtual environment;
2. `fbn --help`, `fbn --version`, and `python -m fbn --help` succeed;
3. sanitized local fixtures cover feed, login, checkpoint, blocked,
   access-denied, empty, and layout-changed states;
4. unit tests prove baseline, restart-safe deduplication, durable pending
   delivery, deterministic ordering, strict schedule validation, redaction, and
   nonzero failure exits;
5. no runtime dependency on `facebook-scraper`, `schedule`, Tenacity,
   browser-use, or a stealth browser remains;
6. no test contacts Facebook;
7. Ruff, pytest, build, Twine validation, clean-wheel smoke tests, and
   `git diff --check` pass; and
8. CI runs on pushes and pull requests, while PyPI publishing remains restricted
   to a deliberate release event.

