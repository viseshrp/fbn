# Changelog

All notable changes to `fbn` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Separate embedded post text when the feed omits both its menu and repeated
  group heading. Require one linked author/date, reject unresolved metadata
  rather than treating it as message text, and preserve strict link verification.
- Cross feed and permalink menu states independently in delivery/restart
  regressions, including malformed headers and unsafe avatar branches.

### Added

- Recognize unlabelled embedded group-post headers in feed and permalink views,
  including missing embedded menus and group/avatar metadata. Only the outer
  post's message is extracted; embedded posts are not additional feed entries.
  Full-message, author, publication and canonical-link checks remain unchanged.
- Recognize closed author/date header assemblies with competing menus as
  non-accepting render failures eligible for the existing bounded in-check retry.
  Unknown headers/controls, incomplete footers and identity failures still stop.
- Record post-menu presence and classified/unclassified header-card counts in
  private structural diagnostics without retaining names, labels or action IDs.
- Ownership-preserving DOM normalization, named body/header/footer recognizers,
  independent `PostView` validation and opt-in same-DOM reference comparison.
- Verified attachment variants for selected/unselected event responses, credited
  profile-picture controls and count-matched photo-album overlays; unrelated
  controls, mismatched credits and counts still fail closed.
- Recognize focus-only credit timestamps, attachment font-icon decoration and
  weekday/clock event labels without stripping any authored message text.
- Parse month/day and day/month attachment dates and composite publication/
  audience labels, rejecting unknown metadata fields.
- Preserve authored text on both sides of a truncated message run during
  expansion, while still requiring exact complete destination text and identity.
- Recognize bounded rich image previews with linked headings and validated
  author/date bylines, without excluding adjacent authored paragraphs.
- Include focus-sign and event-handler-presence categories in private structural
  captures, without retaining handler code or accessible labels.
- Generated wrapper, text-fragment, decoration and discussion variations, with
  explicit rejection tests for unclaimed text, unknown controls and depth limits.
- Compositional attachment ownership for photo-only action shells, including
  nested shell/album regressions and rejection of unrelated sibling controls.
- Normalize typed Bloks layout containers inside owned bodies, while preserving
  section roots and semantic boundaries; cover nested/branching photo albums.
- Content-free, bounded DOM ancestry and block-role snapshots alongside failure
  images, with action phases and late-authentication privacy checks.
- A 128-case preview/panel pipeline matrix plus attachment, dialog, normalization,
  deadline, state-commit, and receipt/restart regression coverage.

- Private, bounded error screenshots of the feed and failing post-link tab,
  captured before tab cleanup with input masking and 10-event retention.
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
- A Compose bootstrap service that receives authentication material through a
  read-only `/run/secrets` mount and gates monitor startup; the monitor service
  never receives that source file.
- Native amd64 and arm64 image CI covering package startup, Chromium
  diagnostics, and sanitized offline DOM extraction.
- A passive scheduler/process health check that never launches a browser,
  opens the authenticated profile, or contacts Facebook.
- A foreground Compose systemd unit that propagates monitor exits, bounds
  unexpected restarts, and leaves recognized hard failures stopped.
- Separate research, specification, architecture, ADR, and implementation-plan
  documents.

### Changed

- Compose media-opening, separate credit/date and event-preview attachment
  ownership before message expansion. Require positive control/content evidence,
  preserve media identifiers/counts, and reject unrelated controls or raw text.
  Cover wrapper/text variants, source/destination equality, atomic rejection,
  delivery and restart; retain fixed control categories in private diagnostics.
- Recover recognized render failures inside the same scheduled check with at
  most three retries after 5/10/20 seconds. Preserve the routine slot, skip
  retries without full-scan/cleanup headroom, recheck after wakeup, and honor
  shutdown, peer schedule changes and restart. Keep rate limits/network failures
  on their separate slower path and all security/identity/layout stops intact.
- Share terminal-state/loading classification across page readiness, sorting,
  post verification and preview expansion. Preserve the actual failure phase,
  capture evidence and route blank-shell failures through bounded in-check
  recovery with fresh scan pages. Keep identity changes and visible unknown
  layouts terminal; do not refresh in a loop or reset action deadlines.
- Classify the current screen even when preview preparation consumes its action
  budget, so a late login/checkpoint or unsupported UI cannot become a retry.
  Preserve phase annotations and classify each error only once.
- Retry stalled preview actions on a recognized unchanged page with the same
  scheduler-owned backoff for browser-waiter and pre-wait budget timeouts;
  unknown layouts, security screens and identity mismatches remain terminal.
- Recheck current security signals at rendering deadlines. A supported view
  arriving between deadline checks aborts for backoff instead of producing an
  empty layout error or being accepted against an earlier screen state.
- Parse normalized body trees before extracting authored text. Collapse only
  presentation wrappers, retain branching/action/ARIA boundaries, prune owned
  discussion and reject unknown meaningful content without changing identity,
  source/destination, catch-up or notification-delivery rules.
- Resolve split preview captions in owned-section DOM order through inert
  wrappers, while refusing to cross discussion, other links, or controls.
  Exclude nested discussion menus at their own boundary rather than discarding
  an enclosing primary-message wrapper.
- Exclude a split Bloks link-preview caption only when a linked image precedes
  its separately clickable domain/title block in the same message section.
  Preserve authored URLs and text, strict preview-prefix and full-destination
  checks, and rejection of unknown controls or ambiguous attachment ownership.
- Treat a blank, non-interactive mobile loading shell as a transient render
  timeout after the existing readiness deadline. Retain private screenshots and
  structural counters, then use bounded in-check recovery instead of stopping
  the monitor. No unbounded reload loop, deadline reset, or weaker link verification;
  unsupported visible layouts and security prompts still fail closed.
- Inspect every matching login/checkpoint control, so hidden fallback forms
  cannot mask a visible security prompt.
- Recover an unreachable archived legacy marker once from the latest delivered
  canonical receipt (schema version 5), preserving the archive and keeping the
  recovered marker fixed across incomplete scans.
- Reuse an already verified destination within a scan only after freshly copying
  the same link and matching the full post again. Log the exact coverage stop
  reason without retaining post content or identities.
- Partition mobile authored messages, attachments, feedback panels, and discussion
  before expansion and verification, replacing the extra-controls reload workaround.
- Recognize image-backed Bloks cards that own preview text and controls without
  a link, while rejecting interactive image-role regions without actual media.
- Keep verified work and the old catch-up marker when the shared scan budget ends.
  Unknown layouts and identity mismatches still stop the whole observation.
- Dismiss only the recognized app-install prompt through Not now, and wait for
  scroll DOM progress instead of sleeping unconditionally.

- Expand shortened mobile feed posts through their own Bloks action region,
  including inline non-button "see more" controls in feeds and post pages.
  Wait for complete text,
  preserve the original post menu/author/date and visible preview prefix, then
  require full equality with the copied-link destination before saving a post.
  Capture the original menu's link before expansion; temporary Bloks action-ID
  refreshes do not change post identity. Log individual expansion comparisons
  on failure without exposing content or action IDs.
- Log structural and terminal-state diagnostics when copied-link verification
  expires after a readable render, including missing scrollers, timestamps and
  action rows. Reclassify late login/account/access/rate-limit screens correctly;
  keep the existing deadline and reject all unverified posts without retrying.
- Verify rounded relative publication ages using their observation times, so an
  hour/day rollover during Copy link does not stop the monitor. Keep exact
  author/body and canonical URL checks, and reject inconsistent or unproven ages.
- Observe permalink identity from navigation onward, rejecting a router change
  to a different post even when it occurs before the first readable render.
- Keep the mobile body boundary at the post's first action row; never substitute
  a reply's row while the real footer is incomplete or deferred. Wait for the
  same permalink to finish rendering within its existing verification deadline.
- Report exact, secret-free post-link comparison failures and distinguish them
  from an unsupported feed in the stopped-monitor alert.
- Use the verified canonical Facebook post ID as the only active post identity.
  Schema version 4 upgrades saved direct links, merges delivery receipts, and
  archives unresolved history without guessing IDs or dropping pending posts.
- Use the mobile reader for monitoring and authentication; keep bundled
  notifications and existing cookies, delivery settings, and scan bounds.
- Resolved hover-only primary post timestamp links within a bounded time
  budget, without clicking or following comment/shared-story links.
- Kept primary story text and its deduplication key stable when a group-feed
  fallback becomes a direct link, preventing duplicate delivery.
- Explicitly labelled group-feed links when no direct post permalink is
  available instead of presenting them as post links.
- Recognized stories owned by a feed wrapper whose article descendants are
  comments or quoted posts. Those descendants cannot replace the main story's
  body or author; an attached comment can supply a trusted parent-post link.
- Enabled secret-free hard-failure notifications in Compose, with recovery
  guidance for layout, authentication, account-action, and delivery failures.
- Migrated secret-free operational logging to human-readable Loguru records
  with monitor lifecycle, browser, scheduling, delivery, and failure-category
  context. The Compose monitor enables verbose logging so those records are
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
- Made `docker compose up` run authentication bootstrap before starting the
  monitor and reuse the existing external profile/state volume by default.
- Made the first non-empty scan a baseline by default, with
  `--notify-initial` as an explicit override.
- Made notification delivery at least once so an Apprise failure survives a
  process restart instead of losing the post.
- Accepted Facebook's positioned feed-item wrapper around one primary semantic
  article while continuing to reject deeper quoted/shared-post permalinks.
- Added photo-only group-post identity handling and parsed rendered publication
  timestamps with `dateparser` in a configured IANA timezone.
- Replaced calendar-day notification eligibility with a durable feed boundary.
  Every unnotified post before the stored marker is queued atomically,
  independent of its parsed publication date.
- Made catch-up scans look beyond the normal sample for the stored post marker.
  When that marker is still missing, visible unnotified posts are queued without
  moving it, so posts exposed by later scans are not silently skipped.
- Excluded nested comment and reply content from post authors and bodies, and
  rejected comment-specific permalinks as post identities.
- Changed `-V` / `--version` to the version flag and reserved `-v` /
  `--verbose` for logging.
- Scoped this version as an unreleased tool for local academic research.

### Removed

- Desktop DOM extraction, timestamp-hover link recovery, temporary content IDs,
  content-fingerprint matching, reader-selection flags, and obsolete fixtures.
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
