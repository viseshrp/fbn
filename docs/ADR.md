# ADR-001: Use a direct persistent browser with bounded auth bootstrap

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
API. Two deployment targets are hard requirements: unattended headless
operation on 64-bit Ubuntu ARM64, including Raspberry Pi 4, and an Ubuntu-based
Docker image for `linux/amd64` and `linux/arm64`.

## Decision

Use official Playwright Python directly.

`fbn bootstrap`, given `--auth-file PATH` and `-i GROUP`, imports an existing
Facebook browser session into a dedicated local user-data directory, entirely
headlessly.
It accepts a Playwright storage-state JSON object, an exported-cookie JSON
array, or Netscape `cookies.txt`. The parser is bounded, imports only
`facebook.com` and subdomain cookies, ignores storage-state origins, and never
prints cookie names or values. It does not depend on undocumented cookie names.
The browser proves the session by opening the requested group and requiring an
authenticated, accessible feed. A failed bootstrap restores the previous
profile cookies.

`fbn check` and `fbn monitor` reuse that persistent context, navigate only to the
requested Facebook group, inspect the visible DOM for canonical post links,
perform a bounded scroll, and close the context after each check. The existing
headed `fbn login` command remains only as an optional recovery path when a user
must personally resolve an account action.

Ubuntu ARM64 and container runtimes use the Playwright-managed regular Chromium
build. Installation is explicit:

```console
python -m playwright install --with-deps chromium
```

The default browser is Playwright Chromium, launched headlessly with
`channel="chromium"`. This selects regular Chromium's current headless
implementation rather than the separate headless-shell executable and keeps the
browser family consistent across bootstrap and monitoring. Installed Chrome,
Edge, and an explicit compatible executable remain opt-in alternatives.

The Docker deployment uses an Ubuntu 24.04 multi-architecture image. It runs as a
non-root user, starts `fbn monitor --browser chromium --headless`, and mounts one
owner-controlled volume for both the dedicated browser profile and SQLite state.
The one-shot Compose bootstrap service receives the authentication file through
a read-only `/run/secrets` mount. The long-running monitor does not receive that
file. Secrets are supplied only at runtime. A container health check is strictly
local: it imports the `fbn` package and does nothing else. It must not invoke a
monitor/check cycle, launch a browser, navigate to Facebook, or open the
authenticated profile, and it does not claim account/group health.
The monitor's Docker restart policy is `no`: its internal scheduler handles
transient retries, while hard-stop failures remain stopped for user action.

Bare-metal and container deployments run the fully noninteractive
`fbn bootstrap` command once against the exact profile directory or volume later
used by the monitor. Authentication values in command arguments or environment
variables, password automation, profile upload, and exposed browser-debugging
ports remain outside the design.

The acquisition layer will:

- use Playwright-managed Chromium headlessly by default, with installed
  Chrome/Edge and an explicit executable path as opt-in alternatives;
- use Playwright-managed regular Chromium as the required Ubuntu ARM64,
  Raspberry Pi 4, and Docker runtime;
- accept authentication only from an explicit local `--auth-file`, never from
  pasted values, a password, or cookie-bearing environment variables;
- constrain bootstrap input to the three documented formats and Facebook
  domains, then validate actual access to the requested group;
- keep browser state and extracted content local.

SQLite will provide durable seen-post state and a notification outbox. Apprise
remains the notification abstraction. A small in-process loop provides the
legacy long-running mode, while a one-shot command supports cron, systemd timers,
and n8n orchestration.

Post identity and freshness are separate decisions. Every supported unseen post
is persisted for deduplication, including photo-only posts identified through a
Facebook `set=gm.<post-id>` photo link. Notification eligibility additionally
requires a recognized rendered Facebook publication timestamp whose calendar
date is today in a configured IANA timezone. The same timezone is applied to the
Playwright context, timestamp parser, and notification boundary. The browser
reconstructs timestamp text from characters actually rendered inside the
timestamp link; off-rectangle decoy characters are excluded. An unknown
timestamp or a timestamp from another calendar day is recorded as seen but does
not enter the notification outbox.

Use `dateparser` for the allowlisted English Facebook timestamp forms, with the
scan time supplied as its explicit relative base. Arrow and Pendulum are more
general date/time libraries but do not directly parse the website-style
relative strings needed here. Maya has more GitHub stars than `dateparser`, but
its latest release is substantially older and it is not selected. `dateparser`
is the most popular actively maintained direct human-date parser evaluated for
this requirement. Standard-library `zoneinfo`, backed by the `tzdata` package,
validates IANA timezone names and performs the final calendar comparison.

## Consequences

### Positive

- Authentication is a complete browser session rather than a pair of repeatedly
  submitted credentials or a partial cookie jar.
- The actual Facebook application renders in a maintained browser engine.
- Browser behavior, extraction, state, scheduling, and delivery are separate and
  testable.
- The dependency set is smaller and the acquisition path is deterministic.
- A user can recover a session manually through the optional headed command.
- Seen posts and pending deliveries survive restarts.
- Reordered, pinned, or newly extractable historical posts do not create false
  "new post" alerts when their Facebook publication date is not today in the
  configured timezone.
- The same browser backend and profile work for headless bootstrap, unattended
  operation, and optional headed recovery.
- Ubuntu ARM64 and Ubuntu-container deployments do not depend on a separately
  packaged Chrome/driver combination.
- One Docker definition can produce native `linux/amd64` and `linux/arm64`
  variants.

### Negative

- Playwright and a browser binary are much larger than a requests-only scraper.
- Browser installation is a separate post-install step.
- The Playwright browser and Ubuntu system packages materially increase install
  and container image size.
- The initial bootstrap needs a highly sensitive session export and a browser
  run that contacts Facebook to validate the requested group.
- Account actions that cannot be represented by a fresh authenticated export
  may still need optional headed recovery on a trusted display.
- A Raspberry Pi 4 has tighter memory, shared-memory, and storage limits than a
  desktop; real native ARM64 browser verification is a validation gate.
- Multi-platform container build/configuration and persistence behavior add
  deployment-specific validation gates.
- DOM changes can break extraction and require a localized selector update.
- Human-date parsing and portable IANA timezone data add runtime dependencies.
- A timestamp layout or locale Facebook has not yet modeled fails closed and
  can suppress a legitimate notification until support is added.
- The visible virtualized feed cannot guarantee that every post appears.
- Users must supply a fresh authentication export or run optional headed
  recovery when the session expires.

## Alternatives considered

### Keep or patch `facebook-scraper`

Rejected. The direct mobile-HTML approach is the limitation being replaced. Its
stale request fingerprint, brittle selectors, incomplete private-group behavior,
and lack of full browser state are architectural rather than a small bug.

### Selenium

Rejected as a second backend. Selenium is mature, but it provides no inherent
account/detection advantage and needs more synchronization/profile boilerplate.
Supporting two engines would double layout and lifecycle testing without a
demonstrated benefit. Selenium Manager also explicitly does not support Linux
ARM64 or Raspberry Pi, so this deployment target would require a separately
managed browser/driver path.

### browser-use

Rejected for the core. An LLM agent is valuable for open-ended tasks, but this
monitor has one constrained workflow. Agent planning adds cost, nondeterminism,
private-content disclosure risk, and a broader set of possible actions.

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

### Patched browser and driver alternatives

Not selected. They add patched browser or driver stacks, fingerprint
configuration, and additional maintenance surfaces without improving the
deterministic monitor architecture.

### Native Facebook notifications only

Not selected as the backend because Facebook does not expose a machine-readable
or complete email/RSS stream for every group post.

## Revisit triggers

Reconsider this decision if:

- Meta offers an officially supported group feed without developer registration;
- native notifications gain a documented complete email/webhook/RSS channel; or
- Playwright drops a required deployment platform and another maintained browser
  driver demonstrably meets the same boundaries;
- the Playwright-managed regular Chromium channel stops working on supported
  native Ubuntu ARM64; or
- the Ubuntu 24.04 base image or browser dependency set no longer produces safe
  native `linux/amd64` and `linux/arm64` containers.

# ADR-002: Use Loguru for human-readable operational logs

- Status: Accepted
- Date: 2026-07-29
- Decision owners: fbn maintainers

## Context

The monitor needs secret-free operational output that is easy to follow both in
an interactive terminal and through `docker compose logs`. The logging layer
must preserve component and event-specific context, remain independent of the
standard-library root logger, support Python 3.10 through 3.14, and avoid
rendering exception locals or other diagnostic data that could contain
credentials.

The viable shortlist was:

- standard-library `logging` with a custom formatter and `LoggerAdapter`;
- Loguru;
- Rich's `RichHandler`;
- `colorlog` or `coloredlogs`; and
- retaining structlog with its console renderer.

## Decision

Use Loguru 0.7.3 and replace structlog completely. Emit one line per event with
a UTC timestamp, level, component, plain-language message, and safe
`key=value` context. Color is enabled only when standard output is an
interactive terminal, so redirected and container logs remain plain.

Configure the sink with `backtrace=False` and `diagnose=False`. This prevents
Loguru's enhanced exception diagnostics from exposing local variables. Continue
to suppress Apprise's standard-library logger, do not change the root logger,
and keep page content, cookie data, authentication paths, profile paths, state
paths, and notification URLs out of logging calls.

## Alternatives considered

### Standard-library logging

Not selected. It has no runtime dependency and excellent interoperability, but
readable contextual output requires a custom adapter, formatter, and handler
configuration. That recreates much of the ergonomics requested from a
human-friendly logging library.

### RichHandler

Not selected. Rich provides the most elaborate terminal presentation and
tracebacks, but those capabilities and its wider rendering dependency stack are
unnecessary for timestamped monitor events. Context fields would still require
custom integration.

### colorlog and coloredlogs

Not selected. They integrate cleanly with standard-library logging but focus on
color rather than contextual logging ergonomics. `colorlog` would still need
the same custom adapter layer; `coloredlogs` has not released since 2021.

### structlog console rendering

Rejected because the requested change is to remove structlog, not merely change
its renderer. It would also retain machine-oriented event calls throughout the
application.

## Consequences

- Operators get concise readable output in terminals and container logs.
- Existing safe contextual counts and categories remain available.
- Loguru adds one small pure-Python runtime dependency.
- Logging configuration owns Loguru's process-wide sink, which is appropriate
  for this CLI application but should be revisited if `fbn` becomes an embedded
  library.
- Loguru calls must remain outside signal handlers because its sinks are not
  reentrant.
