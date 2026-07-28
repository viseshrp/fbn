# ADR-001: Use a direct persistent browser, without stealth features

- Status: Accepted
- Date: 2026-07-28
- Decision owners: fbn maintainers

## Context

`fbn` 0.1 uses `facebook-scraper`, which sends direct requests to Facebook's
mobile HTML. It has no JavaScript execution or durable browser profile, and its
authentication material is reapplied to a requests session. The dependency's
own documentation reports incomplete private-group support and temporary blocks
under repeated scraping.

The desired replacement must remain pip-installable, work with a user's
authenticated group access, preserve low-frequency monitoring and Apprise
notifications, and must not require Meta developer registration or the Graph
API.

Meta's current terms prohibit automated collection without prior permission,
including while logged in, and prohibit bypassing technical controls. No
technical stack can remove that contractual/account risk. The architecture must
therefore improve browser/session reliability without pretending to be
undetectable.

## Decision

Use official Playwright Python directly.

`fbn login` launches a headed persistent context backed by a dedicated local user
data directory. The user performs login and all security challenges manually.
`fbn check` and `fbn monitor` reuse that context, navigate only to the requested
Facebook group, inspect the visible DOM for canonical post links, perform a
bounded scroll, and close the context after each check.

The acquisition layer will:

- use installed Chrome by recommendation, bundled Playwright Chromium/Chrome for
  Testing as a portable option, and an explicit executable path for system
  Chromium;
- never accept a Facebook password or pasted/exported cookie file;
- never set a custom user agent or spoof fingerprint properties;
- never solve or route around a CAPTCHA, checkpoint, consent screen, rate limit,
  or account block;
- never replay internal GraphQL/XHR requests;
- keep browser state and extracted content local; and
- require an explicit risk acknowledgment before browser monitoring.

SQLite will provide durable seen-post state and a notification outbox. Apprise
remains the notification abstraction. A small in-process loop provides the
legacy long-running mode, while a one-shot command supports cron, systemd timers,
and n8n orchestration.

## Consequences

### Positive

- Authentication is a complete browser session rather than a pair of repeatedly
  submitted credentials or a partial cookie jar.
- The actual Facebook application renders in a maintained browser engine.
- Browser behavior, extraction, state, scheduling, and delivery are separate and
  testable.
- The dependency set is smaller and the acquisition path is deterministic.
- A user can inspect headed operation and recover a session manually.
- Seen posts and pending deliveries survive restarts.

### Negative

- Playwright and a browser binary are much larger than a requests-only scraper.
- Browser installation is a separate post-install step.
- A headless server may need Playwright system packages, system Chromium, or a
  virtual display.
- Facebook can still detect or restrict the automation.
- DOM changes can break extraction and require a localized selector update.
- The visible virtualized feed cannot guarantee that every post appears.
- Users must re-run interactive login when the session expires.

## Alternatives considered

### Keep or patch `facebook-scraper`

Rejected. The direct mobile-HTML approach is the limitation being replaced. Its
stale request fingerprint, brittle selectors, incomplete private-group behavior,
and lack of full browser state are architectural rather than a small bug.

### Selenium

Rejected as a second backend. Selenium is mature, but it provides no inherent
account/detection advantage and needs more synchronization/profile boilerplate.
Supporting two engines would double layout and lifecycle testing without a
demonstrated benefit.

### browser-use

Rejected for the core. An LLM agent is valuable for open-ended tasks, but this
monitor has one constrained workflow. Agent planning adds cost, nondeterminism,
private-content disclosure risk, and a broader set of possible actions. Its
hosted stealth/proxy/CAPTCHA features are specifically out of scope.

### n8n

Rejected as the acquisition layer. n8n can schedule `fbn check` and route its
output, but its Facebook integrations use the Graph API and it does not provide
an authenticated, non-Graph group reader. Making a Node/Docker workflow engine a
Python package dependency would also break the packaging goal.

### Crawlee, Apify, or Browserless

Rejected for the local core. Crawlee's crawling queues/retry/proxy machinery is
unnecessary. Apify's maintained Facebook group actor supports public groups only
and explicitly declines private credential use. Remote platforms would require
uploading content or session material. Self-hosted Browserless remains a possible
future execution adapter if its operational cost becomes justified.

### Patchright, Camoufox, undetected-chromedriver, stealth plugins

Rejected. Their central features hide automation signals, inject/rotate
fingerprints, spoof browser properties, or bypass bot controls. That crosses the
project boundary from session reliability into anti-detection circumvention and
creates a fragile dependency on an arms race.

### Native Facebook notifications only

Recommended whenever they satisfy the user's need. Facebook's `All posts`
notification setting and admin moderation alerts are the lowest-risk no-API
options. They are not implemented as the only backend because Meta does not
promise a machine-readable or complete email/RSS stream for every group post.

## Revisit triggers

Reconsider this decision if:

- Meta offers an officially supported group feed without developer registration;
- the project obtains express authorization and a supported data interface;
- native notifications gain a documented complete email/webhook/RSS channel; or
- Playwright drops a required deployment platform and another maintained browser
  driver demonstrably meets the same boundaries.

