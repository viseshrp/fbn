# Implementation plan

## Delivery strategy

The rewrite is split into focused, independently verifiable commits. Each commit
must pass its relevant tests and `git diff --check` before publication.

## Phase 1: research and design

- [x] Audit the current code, packaging, docs, history, and CI.
- [x] Research Playwright, Selenium, browser-use, Crawlee/Apify, Browserless,
  n8n, Patchright, Camoufox, undetected-chromedriver, RSS bridges, and the current
  `facebook-scraper`.
- [x] Verify current Meta automation terms and native notification options.
- [x] Verify Ubuntu ARM64/Raspberry Pi 4 browser support and Ubuntu 24.04
  multi-platform container feasibility against official sources.
- [x] Record the specification, architecture, ADR, research, and this plan.

Verification:

- every external claim links to a primary/official source where available;
- the chosen design contains no stealth, proxy rotation, CAPTCHA solving,
  private API replay, or cloud cookie upload; and
- all four requested design artifacts are separate files.

## Phase 2: package foundation and durable state

- [x] Replace executable `setup.py` metadata with `pyproject.toml`.
- [x] Set version `0.2.0` and Python floor `3.10`.
- [x] Add typed models, configuration, exceptions, platform paths, strict
  duration parsing, and secret redaction.
- [x] Implement the SQLite schema, baseline transaction, seen-post
  deduplication, pending outbox, successful delivery marking, and persisted
  scheduling state.
- [x] Add focused unit tests for configuration and state invariants.

Verification:

- state survives repository re-open/restart tests;
- new state parents are private without changing the mode of an existing parent,
  and the database/WAL/SHM files remain `0600` on Unix;
- a failed delivery remains pending;
- successful delivery clears retained post body/author;
- invalid/zero/negative/partial duration strings fail; and
- an sdist and wheel build from `pyproject.toml`.

## Phase 3: browser and extraction

- [x] Implement validated `GroupRef` and canonical post URL parsing.
- [x] Implement the dedicated profile path and exclusive lock.
- [x] Implement bounded parsing for Playwright storage-state JSON,
  exported-cookie JSON arrays, and Netscape `cookies.txt`.
- [x] Filter to Facebook-domain cookies, ignore origin storage, and keep all
  parser errors free of cookie names and values.
- [x] Implement fully headless `bootstrap` with real group-access validation and
  rollback to the previous profile cookies on failure.
- [x] Retain headed manual login as an optional recovery path.
- [x] Implement Playwright persistent-context launch for installed Chrome/Edge,
  Playwright Chromium, and an explicit executable path.
- [x] Make Playwright Chromium and headless operation the defaults, with headed
  checks/monitoring limited to explicit troubleshooting.
- [x] Implement fail-closed page-state classification.
- [x] Implement bounded semantic DOM extraction and scrolling.
- [x] Add sanitized local HTML fixtures and adapter tests.

Verification:

- tests cover feed, login, checkpoint, consent/CAPTCHA, blocked/rate-limited,
  access denied, explicit empty, and layout-changed pages;
- canonical tracking parameters are removed;
- authentication parsing is bounded, retains no foreign-domain cookie, and
  never prints cookie names or values;
- bootstrap success is based on the requested group's classified page state,
  not on undocumented cookie names;
- failed bootstrap leaves existing profile cookies intact;
- sample and scroll bounds cannot be exceeded;
- no test contacts Facebook; and
- no custom user agent, stealth flag, proxy, or CAPTCHA integration exists.

## Phase 4: orchestration, notification, scheduler, and CLI

- [x] Implement `MonitorService.run_once`.
- [x] Implement plain-text digest rendering, console dry-run, and Apprise result
  checking.
- [x] Implement the bounded long-running scheduler and graceful signals.
- [x] Add `bootstrap`, `login`, `check`, `monitor`, and `doctor` commands.
- [x] Map typed failures to stable nonzero exit codes.
- [x] Preserve supported option/environment names and resolve the `-v` conflict.
- [x] Add CLI and service tests.

Verification:

- first scan baselines without notification;
- later new posts notify in deterministic order;
- notifier failure/restart retries the same pending events;
- auth/checkpoint/layout errors do not loop;
- `-V/--version` and `-v/--verbose` are unambiguous; and
- help/error output never includes session or Apprise secrets.

## Phase 5: documentation, CI, and release readiness

- [x] Rewrite README installation, headless bootstrap, optional login recovery,
  check, monitor, systemd timer, and migration guidance.
- [x] Document native Facebook notifications as the preferred no-automation
  option and the browser monitor's terms/account risk.
- [x] Replace the current release-only smoke workflow with push/PR CI.
- [x] Keep PyPI publication isolated to a deliberate published release.
- [x] Add Ruff, pytest, coverage, build, Twine, `pip check`, and clean-wheel smoke
  verification.
- [ ] Run the entire matrix locally where possible.

## Phase 6: Ubuntu ARM64 and Docker deployment

- [x] Launch Playwright-managed regular Chromium with `channel="chromium"` for
  headless bootstrap and monitoring, plus optional headed recovery.
- [x] Install the browser and Ubuntu dependencies through
  `python -m playwright install --with-deps chromium`.
- [x] Add a sanitized integration test that launches a real headless browser
  without contacting Facebook.
- [x] Add an Ubuntu 24.04 Docker definition and Compose service for native
  `linux/amd64` and `linux/arm64` builds.
- [x] Run the image as a non-root user with one persistent profile/state volume,
  runtime-only configuration/secrets, and an explicit headless monitor command.
- [x] Disable automatic Docker monitor restarts so typed hard-stop failures stay
  stopped; retain transient retry/backoff inside the monitor.
- [x] Add a profile-gated, one-shot Compose bootstrap service that mounts the
  host authentication export read-only at `/run/secrets/facebook_auth`.
- [x] Keep the authentication source out of the monitor service, environment
  variables, image build context, arguments, and layers.
- [x] Keep container health behavior Facebook-independent with only
  `python -c 'import fbn'`; it launches no browser, opens no profile, and makes
  no Facebook request.
- [x] Make initial bootstrap fully headless and noninteractive against the same
  profile/volume used by the monitor; keep headed login optional for recovery.
- [ ] Run the sanitized real-browser integration test on native Ubuntu ARM64 and
  the Raspberry Pi 4 release target.
- [ ] Validate Docker configuration with required runtime values and build/smoke
  test both `linux/amd64` and `linux/arm64` variants.
- [ ] Verify container replacement preserves the profile and SQLite state and
  that image/history/config inspection contains no runtime secret or profile.

Verification:

- the ARM64 test launches Playwright-managed regular Chromium headlessly with
  `channel="chromium"` and loads only sanitized local fixtures;
- the Docker build installs the matching Playwright browser/dependencies on both
  target architectures;
- the runtime UID is non-root and can write only the mounted profile/state
  location required by `fbn`;
- the one-shot bootstrap and unattended monitor resolve the same persistent
  profile path;
- only bootstrap receives the authentication source under `/run/secrets`; the
  monitor has no source-file mount;
- the bare image default is network-inert and the Compose service command is the
  headless monitor;
- no build argument, layer, copied configuration, or image environment contains
  a notification secret or authenticated profile; and
- no image or Compose health command invokes `fbn check`, `fbn monitor`, opens
  the profile, or navigates to Facebook.

Release gates:

- [ ] All acceptance criteria in `docs/SPEC.md` pass.
- [ ] The sanitized browser integration test passes with real
  Playwright-managed Chromium on native Ubuntu ARM64/Raspberry Pi 4 without any
  Facebook request.
- [ ] `docker compose config` succeeds with representative runtime-only values.
- [ ] Both the default and bootstrap Compose profiles validate, and expanded
  configuration proves that only bootstrap receives `facebook_auth`.
- [ ] The Ubuntu 24.04 image builds and passes non-root/local smoke checks for
  both `linux/amd64` and `linux/arm64`.
- [ ] Container replacement preserves the test profile/state volume, and health
  behavior is confirmed not to poll Facebook.
- [ ] `git status --short` contains only intentional files.
- [ ] `git diff --check` passes.
- [ ] The branch is pushed and the remote commit is confirmed.
- [ ] No release or PyPI upload is performed unless separately requested.

## Focused commit sequence

1. `Document browser monitor rewrite`
2. `Add package foundation and durable state`
3. `Add persistent browser acquisition`
4. `Add monitoring CLI and notifications`
5. `Add rewrite test coverage`
6. `Document and automate the browser monitor`
7. `Add Ubuntu ARM64 and Docker deployment`

The exact split may move a test into the same commit as the code it verifies.
No commit should knowingly publish an untested state transition or a broken
package entry point.
