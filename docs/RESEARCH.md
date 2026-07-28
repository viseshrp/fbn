# Research: Facebook group monitoring without the Graph API

Research date: 2026-07-28.

## Bottom line

The existing `facebook-scraper` backend should be replaced. It sends direct HTTP
requests to Facebook's mobile pages, does not execute the site as a browser, and
does not preserve a complete browser session. Its own documentation warns that
private groups may not work and that frequent scraping can trigger temporary
blocks.

The replacement will use official Playwright Python with a dedicated, local,
persistent browser profile. A fully headless bootstrap imports an existing
session from a private local cookie/storage-state file, then validates real
access to the requested group. `fbn` reuses that profile, reads only the visible
group feed DOM, performs a bounded scan, and stops on login, checkpoint,
consent, rate-limit, or block states. A headed login remains an optional
recovery path, not an initial server requirement.

Playwright-managed Chromium and headless execution are the operational defaults.
Installed Chrome/Edge and headed checks remain explicit local alternatives.

Playwright is also the strongest supported route for the required Ubuntu ARM64
and Raspberry Pi 4 deployment. The server runtime uses Playwright-managed
regular Chromium in headless mode, while an Ubuntu 24.04 multi-platform
container provides the same runtime for `linux/amd64` and `linux/arm64`.

This improves session continuity and makes the page load through a normal browser
engine. It does **not** make automation invisible and cannot guarantee that Meta
will not detect or restrict it.

## The non-technical constraint

Meta's current terms are decisive:

- The [Meta Terms of Service](https://www.facebook.com/terms) prohibit automated
  access or data collection without prior permission, including while logged in,
  and prohibit bypassing technological access controls.
- The
  [Automated Data Collection Terms](https://www.facebook.com/legal/automated_data_collection_terms)
  require separate express written permission. Accepting those terms alone is not
  permission.
- [Facebook's robots.txt](https://www.facebook.com/robots.txt) repeats the
  express-permission requirement.

A login cookie proves that a browser session is authenticated. It does not grant
permission to automate collection, expand the account's authorization, or
permit bypassing a control. The rewrite therefore will not claim to be
undetectable and will not contain fingerprint spoofing, CAPTCHA solving, proxy
rotation, hidden GraphQL replay, fake user agents, or other anti-detection
features.

The supported no-API alternative is to enable Facebook's native
[All posts group notifications](https://www.facebook.com/help/187225274663021).
Admins can also use native
[keyword and engagement alerts](https://www.facebook.com/help/279588033089477/).
Users who need a guaranteed, terms-supported data feed must use Meta-authorized
access or a feed supplied by the group administrator.

## Tool comparison

| Tool | Finding | Decision |
| --- | --- | --- |
| [Playwright Python](https://playwright.dev/python/docs/intro) | Pip-installable, actively maintained, supports persistent contexts, installed Chrome, managed Chromium, semantic locators, explicit browser lifecycle control, and current Ubuntu x86-64/ARM64 releases. | Use directly. |
| [Selenium](https://github.com/SeleniumHQ/selenium) | Mature and viable, but offers no detection advantage and requires more profile and synchronization boilerplate. Its official Selenium Manager does not support Linux ARM64 or Raspberry Pi. | Do not add a second browser backend. |
| [browser-use](https://github.com/browser-use/browser-use) | Useful for open-ended, LLM-directed browser tasks and can reuse browser profiles. Its agent loop adds model cost, nondeterminism, a broader action surface, and potential disclosure of private group content to a model or cloud service. Its hosted offering markets stealth, proxies, and CAPTCHA handling. | Do not use in the deterministic monitor core. Revisit only for an attended recovery assistant. |
| [Crawlee Python](https://github.com/apify/crawlee-python) | Strong crawling queues, storage, retry, and proxy infrastructure around Playwright. That machinery is excessive for one bounded group scan, and automatic retries/proxy features conflict with fail-closed behavior. | Do not use. |
| [Apify Facebook Groups Scraper](https://apify.com/apify/facebook-groups-scraper) | The official Apify actor supports public groups only and states that private-group credential use would conflict with Facebook's terms. Cloud execution would also require sending data or session material to a third party. | Do not use for authenticated groups. |
| [Browserless](https://github.com/browserless/browserless) | Can self-host browser/CDP infrastructure with queues and persistence, but adds Docker, operations, and licensing considerations. Cloud use creates the same session-disclosure concern. | Possible future self-hosted execution backend, not a dependency. |
| [n8n](https://github.com/n8n-io/n8n) | Excellent scheduler/orchestrator. Its Facebook nodes use the Graph API and credentials; it does not provide a native, non-Graph private-group reader. | Optional orchestration around `fbn check`, not acquisition. |
| [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) | A Playwright fork whose stated purpose is to be undetected by patching automation signals. | Reject. |
| [Camoufox](https://github.com/daijro/camoufox) | An anti-detect Firefox fork with fingerprint injection/rotation, WebGL and WebRTC spoofing, and humanized input. | Reject. |
| [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) | Explicitly targets bot-mitigation bypass and has a stale PyPI release relative to current Chrome. | Reject. |
| [facebook-scraper](https://github.com/kevinzg/facebook-scraper) | Direct requests/mobile-HTML parser. Last repository commit found was in October 2023; its README warns that private groups may fail and frequent scraping may cause blocks. | Remove. |
| [RSSHub](https://github.com/DIYgod/RSSHub) / [RSS-Bridge](https://github.com/RSS-Bridge/rss-bridge) | No current official Facebook group feed exists. Unofficial Facebook bridges have the same fragile scraping and authorization problem. | Do not depend on them. |

## Ubuntu ARM64 and Raspberry Pi feasibility

The platform choice is based on current primary documentation, not an
assumption that all Linux browser managers support Raspberry Pi:

- Playwright Python's current
  [system requirements](https://playwright.dev/python/docs/intro#system-requirements)
  list Ubuntu 22.04, 24.04, and 26.04 on both x86-64 and ARM64.
- Playwright's official
  [release notes](https://playwright.dev/python/docs/release-notes#ubuntu-arm64-support--more)
  explicitly introduced Ubuntu ARM64 execution in Docker and on Raspberry Pi.
  Its later
  [1.57 notes](https://playwright.dev/python/docs/release-notes#version-157)
  also state that ARM64 Linux continues to use Chromium.
- Canonical's current
  [Raspberry Pi support matrix](https://ubuntu.com/hardware/docs/boards/how-to/ubuntu_supported/raspberry-pi/)
  lists Raspberry Pi 4 support for Ubuntu 22.04, 24.04, and 26.04, including
  64-bit ARM images.
- Selenium's own
  [Selenium Manager documentation](https://www.selenium.dev/documentation/selenium_manager/)
  says its Linux manager is verified only for x64 and does not work on Linux
  ARM64/aarch64 or Raspberry Pi. This does not mean Selenium itself can never be
  wired manually on ARM64, but it makes Selenium Manager unsuitable for the
  required self-contained install path.

The selected Ubuntu ARM64 runtime is therefore:

```console
python -m pip install fbn
python -m playwright install --with-deps chromium
fbn bootstrap \
  --auth-file /private/path/facebook-auth.json \
  -i my-group \
  --browser chromium \
  --acknowledge-automation-risk
fbn monitor --browser chromium --headless ...
```

Playwright's
[browser documentation](https://playwright.dev/python/docs/browsers#chromium-new-headless-mode)
documents `channel="chromium"` as the opt-in to regular Chromium's current
headless implementation rather than the separate headless shell. `fbn` uses
that channel for ARM64 and container bootstrap and monitoring, and the same
managed Chromium for optional headed recovery. This is a supported browser
selection, not webdriver hiding, fingerprint spoofing, or another stealth
technique.

Documentation support is necessary but not sufficient. A release still needs a
sanitized local-fixture test that launches the actual managed browser headlessly
on native Ubuntu ARM64 and the Raspberry Pi 4 target. No live Facebook account,
cookie, or request belongs in that test.

## Ubuntu container feasibility

The Docker design also follows primary platform guidance:

- The Docker Official
  [Ubuntu image](https://hub.docker.com/_/ubuntu/) publishes Ubuntu 24.04 for
  `amd64` and `arm64v8` among its supported architectures.
- Docker's
  [multi-platform build documentation](https://docs.docker.com/build/building/multi-platform/)
  describes one manifest containing `linux/amd64` and `linux/arm64` variants
  and specifically notes automatic ARM variant selection on a Raspberry Pi.
- Playwright's official
  [Docker guidance](https://playwright.dev/python/docs/docker) uses Ubuntu 24.04
  (`noble`), requires the Python package plus browser and system dependencies,
  and recommends a separate non-root browser user for crawling/scraping
  workloads.
- Playwright's
  [browser installation guide](https://playwright.dev/python/docs/browsers#install-system-dependencies)
  supports installing the matching browser and Linux packages together with
  `playwright install --with-deps chromium`.

`fbn` therefore builds its own Ubuntu 24.04 image for both target architectures,
pins the Playwright package/browser relationship, and launches the monitor as a
dedicated non-root user. The image contains no Facebook authentication or
Apprise secret. A named volume persists the dedicated browser profile and SQLite
state across container replacement.

The profile is initialized by a fully headless, one-shot `fbn bootstrap`
invocation using that same volume. Compose mounts the host authentication export
read-only at `/run/secrets/facebook_auth` only in the profile-gated bootstrap
service. The long-running monitor never receives that source file. Password
automation, cookie values in environment variables, uploaded browser profiles,
and exposed remote-debugging ports remain out of scope.

Container liveness must remain independent of Facebook. The selected health
probe only imports the installed `fbn` package. It does not invoke `fbn check`
or `fbn monitor`, launch a browser, open the authenticated profile, or navigate
to Facebook. A Facebook poll would add collection traffic, contend for the
profile lock, and still fail to distinguish container health from session or
site health.

## Why a dedicated browser profile

Playwright's
[`launch_persistent_context`](https://playwright.dev/python/docs/api/class-browsertype)
stores cookies, local storage, IndexedDB, and other browser state in a user data
directory. This avoids a fresh programmatic login on every poll.

The profile must be:

- dedicated to `fbn`, not the user's ordinary Chrome profile;
- stored outside the repository;
- readable only by the local user where the operating system supports Unix
  permissions;
- stored in an owner-controlled persistent volume when containerized;
- used by one `fbn` process at a time; and
- bootstrapped headlessly from a private, explicit authentication file and
  validated against the requested group.

Playwright warns that authentication state can be used to impersonate the user.
For that reason, `fbn` accepts only a local file path, imports only Facebook
domains, and will not accept pasted cookies, cookie values in command arguments
or environment variables, or cloud profile uploads. The supported parser reads
Playwright storage-state JSON cookies, exported-cookie JSON arrays, or Netscape
`cookies.txt`; it ignores storage-state origins and never prints cookie names or
values. See Playwright's
[authentication guidance](https://playwright.dev/python/docs/auth).

Chrome 136 and later also restrict remote debugging against the default profile.
Google recommends a non-standard user data directory. That reinforces the
dedicated-profile choice; see
[Chrome's remote-debugging security change](https://developer.chrome.com/blog/remote-debugging-port).

## Extraction strategy

Facebook-generated class names are not a stable contract. The extractor will:

1. open only an `https://www.facebook.com/groups/...` URL;
2. wait for a signed-in page state;
3. find canonical group post or permalink anchors;
4. derive identity from the group and post IDs in those URLs;
5. scope visible text and an optional author to the nearest semantic post
   container;
6. scroll a bounded number of times until the requested sample is reached or no
   new post IDs appear; and
7. deduplicate by `(group_key, post_id)`.

Playwright recommends user-facing and ARIA locators over structural CSS/XPath
chains in its [locator guide](https://playwright.dev/python/docs/locators).
Selectors and extraction JavaScript will live in one module and be tested against
sanitized local fixtures.

## Operational boundaries

- Default cadence remains a randomized 1–3 hour range.
- A scan is capped by post count, scroll count, and navigation timeout.
- No retry occurs around a login, checkpoint, consent, CAPTCHA, account block, or
  changed/unsupported layout.
- Transient navigation failures use bounded backoff in the long-running monitor.
- Docker does not restart a failed monitor automatically: internal backoff owns
  transient retries, while hard-stop exits remain stopped for user action.
- The first successful scan establishes a baseline and sends no historical flood.
- SQLite persists seen IDs and a notification outbox across restarts.
- A post is marked delivered only after Apprise reports success.
- Diagnostic HTML, traces, and screenshots are not captured by default because
  they may contain private group content.
- Ubuntu ARM64 and Docker monitoring uses Playwright-managed regular Chromium
  with `channel="chromium"` in headless mode.
- Initial authentication uses the bounded headless secret-file bootstrap
  against the exact profile or volume used by the monitor.
- Headed login remains available only for optional recovery on a trusted
  display.
- Docker builds contain no runtime secret or authenticated browser profile, and
  the container runs as non-root.
- The Docker monitor has no authentication-file mount; only the one-shot
  bootstrap service receives the file under `/run/secrets`.
- Container health behavior is local-only and never causes a Facebook request.
