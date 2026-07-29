# Changelog

All notable changes to `fbn` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Playwright browser acquisition with a dedicated persistent local profile.
- Fully headless `fbn bootstrap` from an explicit authentication file, with
  support for Playwright storage-state JSON, exported-cookie JSON arrays, and
  Netscape `cookies.txt`.
- Optional `fbn login` for headed, user-controlled account recovery.
- `fbn check`, `fbn monitor`, and read-only `fbn doctor` commands.
- Bounded feed navigation, semantic DOM extraction, and fail-closed page-state
  classification.
- SQLite-backed baseline, seen-post history, pending notification outbox, and
  persisted scheduling state.
- Deterministic plain-text Apprise notifications and a console dry-run mode.
- Randomized 1–3 hour default scheduling, a 15-minute to 365-day accepted
  interval range, and bounded transient-failure backoff.
- Python 3.10–3.14 testing and sanitized local-browser integration coverage.
- Ubuntu 24.04 Docker and Compose deployment for `linux/amd64` and
  `linux/arm64`, including 64-bit Raspberry Pi 4, with a non-root runtime,
  Playwright-managed Chromium, and a persistent local profile/state volume.
- A profile-gated Compose bootstrap service that receives authentication
  material through a read-only `/run/secrets` mount; the monitor service never
  receives that source file.
- Native amd64 and arm64 image CI covering package startup, Chromium
  diagnostics, and sanitized offline DOM extraction.
- A container-local package-import health check that never launches a browser,
  opens the authenticated profile, or contacts Facebook.
- Separate research, specification, architecture, ADR, and implementation-plan
  documents.

### Changed

- Migrated secret-free operational logging to `structlog` JSON records with
  monitor lifecycle, browser, scheduling, delivery, and failure-category
  fields. The Compose monitor enables verbose logging so those records are
  available through container logs.
- Replaced direct mobile-page requests with a complete browser session rendered
  by Chrome, Edge, Playwright Chromium, or an explicit Chromium executable.
- Replaced executable `setup.py` packaging with `pyproject.toml`.
- Changed the CLI from one command to explicit `bootstrap`, `login`, `check`,
  `monitor`, and `doctor` workflows.
- Made Playwright Chromium and headless operation the defaults; `--headed` is an
  explicit check/monitor troubleshooting mode.
- Disabled automatic Docker monitor restarts so fail-closed account, access,
  profile, and layout exits remain stopped instead of looping.
- Made the first non-empty scan a baseline by default, with
  `--notify-initial` as an explicit override.
- Made notification delivery at least once so an Apprise failure survives a
  process restart instead of losing the post.
- Accepted Facebook's positioned feed-item wrapper around one primary semantic
  article while continuing to reject deeper quoted/shared-post permalinks.
- Added photo-only group-post identity handling and a rendered publication-time
  gate so newly discovered historical posts are recorded without being
  announced as new. `dateparser` interprets Facebook's relative timestamps in a
  configured IANA timezone, and notifications require the publication date to
  match the current calendar day.
- Changed `-V` / `--version` to the version flag and reserved `-v` /
  `--verbose` for logging.
- Scoped this version as an unreleased tool for local academic research.

### Removed

- `facebook-scraper`, `schedule`, and Tenacity runtime dependencies.
- Facebook username, password, per-monitor cookie-file, and custom-user-agent
  options.
- The automation-risk acknowledgment option and environment variable.
- The package-publication workflow.
- Process-only seen-post state.

### Security

- Browser profile and SQLite paths default to private per-user application data.
- Newly created Unix state parents are private, existing parent modes are
  preserved, and SQLite database/WAL/SHM files are forced to owner-only access.
- Authentication, checkpoint, consent, CAPTCHA, access-denied, and unsupported
  layout states stop without being retried indefinitely.
- Authentication bootstrap imports only `facebook.com` cookies from a bounded
  local file, validates real access to the requested group headlessly, ignores
  storage-state origins, and never prints cookie names or values.
- Apprise URLs are redacted from errors, and successful delivery clears pending
  post author/body content from the outbox.
- CI and tests use no Facebook credentials, cookies, private group content, or
  live Facebook requests.
- Container images contain no notification secrets, login material, browser
  profile, SQLite state, or captured group content.

[Unreleased]: https://github.com/viseshrp/fbn/commits/main
