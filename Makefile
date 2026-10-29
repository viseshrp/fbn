SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= python
BASH ?= bash

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available make targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: version
version: ## Print the current package version
	@sed -n 's/^__version__ = "\([^"]*\)"$$/\1/p' fbn/__init__.py

.PHONY: check-version
check-version: ## Verify RELEASE_TAG matches the package version
	@version="$$(sed -n 's/^__version__ = "\([^"]*\)"$$/\1/p' fbn/__init__.py)"; \
	if [ -z "$${RELEASE_TAG:-}" ]; then \
		echo "ERROR: RELEASE_TAG is required (for example RELEASE_TAG=v$$version)." >&2; \
		exit 1; \
	fi; \
	if [ "$${RELEASE_TAG}" != "v$$version" ] && [ "$${RELEASE_TAG}" != "$$version" ]; then \
		echo "ERROR: Release tag '$${RELEASE_TAG}' does not match package version '$$version'." >&2; \
		exit 1; \
	fi; \
	echo "Release tag '$${RELEASE_TAG}' matches package version '$$version'."

.PHONY: install
install: ## Install built package from dist/
	$(PYTHON) -m pip uninstall fbn -y
	$(PYTHON) -m pip install --find-links dist fbn

.PHONY: install-dev
install-dev: ## Install package and development dependencies in editable mode
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

.PHONY: install-browser
install-browser: ## Install Playwright Chromium and Linux dependencies
	$(PYTHON) -m playwright install --with-deps chromium

.PHONY: check-deps
check-deps: ## Check installed Python dependency consistency
	$(PYTHON) -m pip check

.PHONY: lint
lint: ## Run Ruff lint, format check, and git whitespace check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	git diff --check

.PHONY: check
check: lint ## Alias for lint

.PHONY: test-unit
test-unit: ## Run unit tests with coverage (excluding browser integration)
	$(PYTHON) -m pytest --ignore=tests/test_browser_integration.py --cov=fbn --cov-report=term-missing

.PHONY: test-browser
test-browser: ## Run sanitized local-page Playwright browser integration tests
	$(PYTHON) -m pytest tests/test_browser_integration.py -q

.PHONY: test
test: ## Run the full pytest suite
	$(PYTHON) -m pytest

.PHONY: build
build: ## Build source and wheel distributions into dist/
	$(PYTHON) -m build

.PHONY: check-dist
check-dist: ## Validate built distributions in dist/ with Twine
	$(PYTHON) -m twine check dist/*

.PHONY: smoke-wheel
smoke-wheel: ## Install built wheel in a clean temporary venv and smoke-test CLI
	@smoke_dir="$$(mktemp -d "$${RUNNER_TEMP:-$${TMPDIR:-/tmp}}/fbn-wheel-smoke.XXXXXXXX")"; \
	trap 'rm -rf -- "'"$$smoke_dir"'"' EXIT; \
	$(PYTHON) -m venv "$$smoke_dir/venv"; \
	"$$smoke_dir/venv/bin/python" -m pip install dist/*.whl; \
	( \
		cd "$$smoke_dir"; \
		"$$smoke_dir/venv/bin/python" -m pip check; \
		"$$smoke_dir/venv/bin/fbn" --help >/dev/null; \
		"$$smoke_dir/venv/bin/fbn" --version; \
		"$$smoke_dir/venv/bin/python" -m fbn --help >/dev/null \
	)

.PHONY: smoketest
smoketest: ## Smoke-test the installed fbn CLI
	fbn --help
	fbn --version

.PHONY: clean
clean: ## Remove build, dist, cache, coverage, and bytecode artifacts
	rm -rf build dist .pytest_cache .ruff_cache htmlcov .coverage
	rm -rf *.egg-info
	find . -name '*.pyc' -delete

.PHONY: docker-config
docker-config: ## Validate compose.yaml, docker-compose.yml, and deploy/fbn.service
	$(BASH) scripts/docker.sh config

.PHONY: docker-build
docker-build: ## Build the fbn container image locally
	$(BASH) scripts/docker.sh build

.PHONY: docker-pull
docker-pull: ## Pull the prebuilt fbn container image from GHCR
	$(BASH) scripts/docker.sh pull

.PHONY: docker-smoke
docker-smoke: ## Run offline container smoke, screenshot, and persistence checks
	$(BASH) scripts/docker.sh smoke

.PHONY: docker-test
docker-test: ## Run lint, unit, build, wheel, and browser tests in a disposable container
	$(BASH) scripts/docker.sh test

.PHONY: docker-test-matrix
docker-test-matrix: ## Run unit tests across Python 3.10-3.14 in disposable containers
	$(BASH) scripts/docker.sh test-matrix

.PHONY: docker-doctor
docker-doctor: ## Run read-only container browser diagnostics
	$(BASH) scripts/docker.sh doctor

.PHONY: docker-bootstrap
docker-bootstrap: ## Import and validate Facebook cookies into the persistent Docker volume
	$(BASH) scripts/docker.sh bootstrap

.PHONY: docker-check
docker-check: ## Run a one-shot group check in Docker
	$(BASH) scripts/docker.sh check

.PHONY: docker-check-dry
docker-check-dry: ## Run a one-shot dry-run group check in Docker (no notifications sent)
	DRY_RUN=1 $(BASH) scripts/docker.sh check

.PHONY: docker-up
docker-up: ## Bootstrap authentication and start the background monitor in Docker
	$(BASH) scripts/docker.sh up

.PHONY: docker-run
docker-run: docker-up ## Alias for docker-up

.PHONY: up
up: docker-up ## Alias for docker-up

.PHONY: docker-down
docker-down: ## Stop the Docker Compose monitor (preserving your persistent volume)
	$(BASH) scripts/docker.sh down

.PHONY: down
down: docker-down ## Alias for docker-down

.PHONY: docker-logs
docker-logs: ## Follow live Docker Compose monitor logs
	$(BASH) scripts/docker.sh logs

.PHONY: logs
logs: docker-logs ## Alias for docker-logs

.PHONY: docker-status
docker-status: ## Show Docker Compose container status
	$(BASH) scripts/docker.sh status

.PHONY: status
status: docker-status ## Alias for docker-status

.PHONY: docker-push
docker-push: ## Smoke-test, then build and push multi-arch images to GHCR as TAGS
	$(BASH) scripts/docker.sh push

.PHONY: docker-push-digest
docker-push-digest: ## Push a single-platform container image by digest to GHCR
	$(BASH) scripts/docker.sh push-digest

.PHONY: docker-publish-manifest
docker-publish-manifest: ## Publish and verify a multi-arch GHCR manifest from verified digests
	$(BASH) scripts/docker.sh publish-manifest
