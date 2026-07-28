# Research: Facebook group monitoring without the Graph API

Research date: 2026-07-28.

## Bottom line

The existing `facebook-scraper` backend should be replaced. It sends direct HTTP
requests to Facebook's mobile pages, does not execute the site as a browser, and
does not preserve a complete browser session. Its own documentation warns that
private groups may not work and that frequent scraping can trigger temporary
blocks.

The replacement will use official Playwright Python with a dedicated, local,
persistent browser profile. The user signs in interactively in a headed browser.
`fbn` then reuses that profile, reads only the visible group feed DOM, performs a
bounded scan, and stops on login, checkpoint, consent, rate-limit, or block
states.

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
permission to automate collection. The rewrite therefore will not claim to be
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
| [Playwright Python](https://github.com/microsoft/playwright-python) | Pip-installable, actively maintained, supports persistent contexts, installed Chrome, bundled Chromium/Chrome for Testing, semantic locators, and explicit browser lifecycle control. | Use directly. |
| [Selenium](https://github.com/SeleniumHQ/selenium) | Mature and viable, but offers no detection advantage and requires more profile and synchronization boilerplate for this use case. | Do not add a second browser backend. |
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
- used by one `fbn` process at a time; and
- authenticated by the user in a visible browser, including any 2FA or consent
  steps.

Playwright warns that authentication state can be used to impersonate the user.
For that reason, `fbn` will not accept pasted cookies, cookie values in command
arguments, or cloud profile uploads. See Playwright's
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
- The first successful scan establishes a baseline and sends no historical flood.
- SQLite persists seen IDs and a notification outbox across restarts.
- A post is marked delivered only after Apprise reports success.
- Diagnostic HTML, traces, and screenshots are not captured by default because
  they may contain private group content.

