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
- [x] Record the specification, architecture, ADR, research, and this plan.

Verification:

- every external claim links to a primary/official source where available;
- the chosen design contains no stealth, proxy rotation, CAPTCHA solving,
  private API replay, or cloud cookie upload; and
- all four requested design artifacts are separate files.

## Phase 2: package foundation and durable state

- [ ] Replace executable `setup.py` metadata with `pyproject.toml`.
- [ ] Set version `0.2.0` and Python floor `3.10`.
- [ ] Add typed models, configuration, exceptions, platform paths, strict
  duration parsing, and secret redaction.
- [ ] Implement the SQLite schema, baseline transaction, seen-post
  deduplication, pending outbox, successful delivery marking, and persisted
  scheduling state.
- [ ] Add focused unit tests for configuration and state invariants.

Verification:

- state survives repository re-open/restart tests;
- a failed delivery remains pending;
- successful delivery clears retained post body/author;
- invalid/zero/negative/partial duration strings fail; and
- an sdist and wheel build from `pyproject.toml`.

## Phase 3: browser and extraction

- [ ] Implement validated `GroupRef` and canonical post URL parsing.
- [ ] Implement the dedicated profile path and exclusive lock.
- [ ] Implement headed manual login.
- [ ] Implement Playwright persistent-context launch for installed Chrome/Edge,
  Playwright Chromium, and an explicit executable path.
- [ ] Implement fail-closed page-state classification.
- [ ] Implement bounded semantic DOM extraction and scrolling.
- [ ] Add sanitized local HTML fixtures and adapter tests.

Verification:

- tests cover feed, login, checkpoint, consent/CAPTCHA, blocked/rate-limited,
  access denied, explicit empty, and layout-changed pages;
- canonical tracking parameters are removed;
- sample and scroll bounds cannot be exceeded;
- no test contacts Facebook; and
- no custom user agent, stealth flag, proxy, or CAPTCHA integration exists.

## Phase 4: orchestration, notification, scheduler, and CLI

- [ ] Implement `MonitorService.run_once`.
- [ ] Implement plain-text digest rendering, console dry-run, and Apprise result
  checking.
- [ ] Implement the bounded long-running scheduler and graceful signals.
- [ ] Add `login`, `check`, `monitor`, and `doctor` commands.
- [ ] Map typed failures to stable nonzero exit codes.
- [ ] Preserve supported option/environment names and resolve the `-v` conflict.
- [ ] Add CLI and service tests.

Verification:

- first scan baselines without notification;
- later new posts notify in deterministic order;
- notifier failure/restart retries the same pending events;
- auth/checkpoint/layout errors do not loop;
- `-V/--version` and `-v/--verbose` are unambiguous; and
- help/error output never includes session or Apprise secrets.

## Phase 5: documentation, CI, and release readiness

- [ ] Rewrite README installation, login, check, monitor, systemd timer, and
  migration guidance.
- [ ] Document native Facebook notifications as the preferred no-automation
  option and the browser monitor's terms/account risk.
- [ ] Replace the current release-only smoke workflow with push/PR CI.
- [ ] Keep PyPI publication isolated to a deliberate published release.
- [ ] Add Ruff, pytest, coverage, build, Twine, `pip check`, and clean-wheel smoke
  verification.
- [ ] Run the entire matrix locally where possible.

Release gates:

- all acceptance criteria in `docs/SPEC.md` pass;
- `git status --short` contains only intentional files;
- `git diff --check` passes;
- the branch is pushed and the remote commit is confirmed; and
- no release or PyPI upload is performed unless separately requested.

## Focused commit sequence

1. `Document browser monitor rewrite`
2. `Add package foundation and durable state`
3. `Add persistent browser acquisition`
4. `Add monitoring CLI and notifications`
5. `Add rewrite test coverage`
6. `Document and automate the browser monitor`

The exact split may move a test into the same commit as the code it verifies.
No commit should knowingly publish an untested state transition or a broken
package entry point.

