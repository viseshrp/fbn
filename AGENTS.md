# Repository Agent Guide

## Scope and working style

This file applies to the entire repository. Follow a more specific nested
`AGENTS.md` if one is added later. Preserve the user's current work: inspect the
working tree before editing, keep changes narrowly tied to the request, and do
not rewrite, revert, stage, or commit unrelated modifications.

Treat `fbn` as a security-sensitive research tool. Never expose Facebook
cookies, browser profiles, state databases, notification URLs, `.env` contents,
or other credentials in source, fixtures, commands, logs, test output, commits,
or reports. Tests must use synthetic data and local sanitized fixtures; they
must never contact Facebook or send a real notification.

## Main-branch-only workflow

All work must be performed directly on `main`. Before making changes, verify
that the checkout is on `main`; remain there for editing, validation, commits,
and pushes. This is an experimental project, so do not create, switch to, or
publish feature, fix, topic, release, `agent/*`, or other auxiliary branches.
Do not create branch-based worktrees or open branch-based pull requests.

Every task that changes repository files must end with a focused commit
containing only that task's changes and a push to `main`, unless the user
explicitly says not to commit or push. Do not leave completed agent changes
only in the working tree, bundle unrelated work, or force-push.

If `main` is unavailable, protected, or contains work that cannot be preserved
safely, stop and report the blocker. Never create another branch as a
workaround, and never delete or rewrite existing branches unless the user
explicitly requests it.

## Mandatory Docker-only local validation

**Every local test or executable validation must run inside a Docker
container, never directly on the host.** This includes Python, `pip`, `pytest`,
Ruff, build/Twine checks, Playwright, package smoke tests, and repository
`Makefile` test targets. Do not use a host virtual environment or fall back to
host execution because it is faster or Docker is unavailable. The host may be
used only to inspect/edit files, inspect Git state, and invoke Docker. If Docker
cannot run, stop and report validation as not run.

- Build from the current checkout so tests exercise the actual changes.
- The deployment image does not contain the test suite or development tools.
  Use a disposable test image/stage or mount the checkout read-only into a
  disposable container and install dependencies only into container-owned
  storage. Never install test dependencies on the host.
- Prefer a non-root container, read-only source mounts, and container-local
  temporary/cache/output directories. Do not let tests create host-owned
  `.pytest_cache`, `.ruff_cache`, coverage, build, or bytecode artifacts.
- Disable the test container's network with `--network none` after any
  explicitly required dependency installation. Never mount the real `.env`,
  application-data directory, browser profile, state database, Docker socket,
  SSH agent, cloud credentials, or a deployment `fbn-data` volume.
- Use the smallest relevant test first, then run every applicable CI-equivalent
  check before calling the change ready. The CI contract currently includes:
  unit tests on Python 3.10–3.14; dependency consistency; Ruff lint and format
  checks; distribution build, Twine validation, and clean-wheel smoke tests;
  sanitized Playwright integration tests; and native-architecture Docker image
  checks. A reduced set for a documentation-only change is acceptable only when
  the final report says exactly what was and was not run.

## Cleanup is part of every test

Plan cleanup before starting a test and make it run on success, failure,
timeout, or interruption.

- Give each run a unique test-only name/project label. Use `docker run --rm`
  where possible and an exit trap for every other resource.
- For Compose tests, use a unique `--project-name`; always finish with
  `docker compose --project-name <test-project> down --volumes
  --remove-orphans`.
- Stop and remove every test container, then remove the exact test-only
  networks, volumes, images, builders/caches, and temporary files created by
  that run. Verify those named resources no longer exist and check that the
  working tree gained no test artifacts.
- Never use broad cleanup such as `docker system prune`, and never remove or
  modify shared images, unrelated containers, or persistent application
  volumes. In particular, protect every deployment `fbn-data` volume because it
  may contain authentication and state.
- Cleanup failures are not optional warnings: resolve them before finishing, or
  clearly report the exact leftover resource and why it could not be removed.

## Handoff

Report the container/image and exact commands used, the passed, failed, and
skipped checks, and confirmation that test resources were cleaned up. Never
claim a check passed when it was skipped, could not start, or ran only on the
host.
