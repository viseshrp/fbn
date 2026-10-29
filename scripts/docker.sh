#!/usr/bin/env bash
set -euo pipefail

# Git Bash on Windows would otherwise rewrite container paths and mount specs.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_REPO="ghcr.io/viseshrp/fbn"
DEFAULT_IMAGE="${DEFAULT_REPO}:local"
DEFAULT_REMOTE_IMAGE="${DEFAULT_REPO}:latest"

usage() {
  cat <<'EOF'
Usage: scripts/docker.sh <command>

User commands:
  up                Bootstrap authentication and start the monitor in the background
  down              Stop the monitor container (preserving your profile/state volume)
  logs              Follow live container logs
  status            Show Compose container status
  bootstrap         Import/validate Facebook cookies into the persistent volume
  check             Run a single group check in Docker (set DRY_RUN=1 for a dry run)
  doctor            Run read-only container browser diagnostics
  pull              Pull the prebuilt container image from GHCR
  build             Build the container image locally from this checkout

Validation & CI commands:
  config            Validate compose.yaml, docker-compose.yml, and deploy/fbn.service
  smoke             Run offline container smoke, screenshot, and persistence checks
  test              Run lint, unit, build, wheel, and browser tests in a disposable container
  test-matrix       Run unit tests across Python 3.10-3.14 in disposable containers
  push              Smoke-test, then build and push multi-arch images to GHCR as TAGS
  push-digest       Push a single-platform image by digest to GHCR
  publish-manifest  Create and verify a multi-arch GHCR manifest from verified digests
EOF
}

resolve_base_version() {
  sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "${REPO_ROOT}/fbn/__init__.py"
}

resolve_version() {
  if [[ -n "${DOCKER_VERSION:-}" ]]; then
    printf '%s\n' "${DOCKER_VERSION}"
  else
    resolve_base_version
  fi
}

resolve_revision() {
  if [[ -n "${DOCKER_VCS_REF:-}" ]]; then
    printf '%s\n' "${DOCKER_VCS_REF}"
  else
    git -C "${REPO_ROOT}" rev-parse HEAD
  fi
}

expect_equal() {
  if [[ "$2" != "$3" ]]; then
    echo "ERROR: $1: expected '$3', got '$2'" >&2
    return 1
  fi
}

host_path() {
  if [[ "${OSTYPE:-}" == msys* ]]; then
    (cd "$1" && pwd -W)
  else
    printf '%s\n' "$1"
  fi
}

load_dotenv_defaults() {
  local env_file="${REPO_ROOT}/.env"
  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    if [[ -z "${line}" || "${line}" == \#* || "${line}" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value}" =~ ^\".*\"$ || "${value}" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    case "${key}" in
      FBN_GROUP|FBN_APPRISE_URL|FBN_AUTH_FILE|FBN_DATA_VOLUME|FBN_IMAGE|FBN_TIMEZONE|FBN_EVERY|FBN_TO|FBN_UID|FBN_GID)
        if [[ -z "${!key:-}" ]]; then
          export "${key}=${value}"
        fi
        ;;
    esac
  done < "${env_file}"
}

ensure_data_volume() {
  local volume="${FBN_DATA_VOLUME:-fbn-monitor-data}"
  docker volume create "${volume}" >/dev/null
}

ensure_runtime_image() {
  local image="${DOCKER_IMAGE:-${FBN_IMAGE:-${DEFAULT_REMOTE_IMAGE}}}"
  if [[ "${DOCKER_BUILD:-0}" == "1" || "${image}" == "${DEFAULT_IMAGE}" || "${image}" == "fbn:local" ]]; then
    DOCKER_IMAGE="${image}" cmd_build >&2
  elif ! docker image inspect "${image}" >/dev/null 2>&1; then
    echo "Pulling ${image}..." >&2
    if ! docker pull "${image}" >&2; then
      echo "Could not pull ${image}; building locally instead..." >&2
      DOCKER_IMAGE="${image}" cmd_build >&2
    fi
  fi
  printf '%s\n' "${image}"
}

ghcr_login() {
  if [[ -n "${GH_TOKEN:-}" ]]; then
    printf '%s\n' "${GH_TOKEN}" | docker login ghcr.io -u "${DOCKER_USERNAME:-viseshrp}" --password-stdin
  elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh auth token | docker login ghcr.io -u "${DOCKER_USERNAME:-viseshrp}" --password-stdin
  fi
}

cmd_config() {
  echo "Validating Compose and systemd configuration..."
  (
    cd "${REPO_ROOT}"
    test -L docker-compose.yml
    test "$(readlink docker-compose.yml)" = compose.yaml
    grep -F 'ExecStart=/usr/bin/docker compose --file compose.yaml up ' deploy/fbn.service >/dev/null
    grep -F 'ExecStop=/usr/bin/docker compose --file compose.yaml stop ' deploy/fbn.service >/dev/null

    local legacy_json current_json
    legacy_json="$(
      FBN_APPRISE_URL="json://127.0.0.1:9" \
      FBN_AUTH_FILE="/dev/null" \
      FBN_GROUP="test-group" \
      COMPOSE_FILE=docker-compose.yml \
      docker compose --env-file /dev/null config --format json
    )"
    current_json="$(
      FBN_APPRISE_URL="json://127.0.0.1:9" \
      FBN_AUTH_FILE="/dev/null" \
      FBN_GROUP="test-group" \
      docker compose --file compose.yaml --env-file /dev/null config --format json
    )"
    expect_equal "docker-compose.yml vs compose.yaml config" "${legacy_json}" "${current_json}"

    FBN_APPRISE_URL="json://127.0.0.1:9" \
    FBN_AUTH_FILE="/dev/null" \
    FBN_GROUP="test-group" \
    docker compose --file compose.yaml --env-file /dev/null config --quiet

    jq -e '
      .services.bootstrap as $bootstrap |
      .services.fbn as $monitor |
      $bootstrap.secrets[0] as $secret |
      ($bootstrap.image == "ghcr.io/viseshrp/fbn:latest") and
      ($monitor.image == "ghcr.io/viseshrp/fbn:latest") and
      ($bootstrap.command[0:4] == ["bootstrap", "--auth-file", "/run/secrets/facebook_auth", "-i"]) and
      ($monitor.command | index("--verbose") != null) and
      ($monitor.command | index("--include-errors") != null) and
      ($bootstrap.healthcheck.disable == true) and
      ($bootstrap.restart == "no") and
      ($monitor.restart == "no") and
      (($bootstrap.mem_limit | tonumber) == 1073741824) and
      (($monitor.mem_limit | tonumber) == 1073741824) and
      (($bootstrap.memswap_limit | tonumber) == 1073741824) and
      (($monitor.memswap_limit | tonumber) == 1073741824) and
      ($bootstrap.stop_grace_period == "2m0s") and
      ($monitor.stop_grace_period == "2m0s") and
      ($monitor.depends_on.bootstrap.condition == "service_completed_successfully") and
      (($monitor | has("secrets")) | not) and
      ($secret.source == "facebook_auth") and
      ($secret.target == "facebook_auth") and
      ($bootstrap.volumes[0].source == "fbn-data") and
      ($monitor.volumes[0].source == "fbn-data") and
      (.volumes["fbn-data"].external == true)
    ' <<<"${current_json}" >/dev/null
  )
  echo "Compose configuration valid."
}

cmd_build() {
  local image="${DOCKER_IMAGE:-${DEFAULT_IMAGE}}"
  local version
  version="$(resolve_version)"
  local revision
  revision="$(resolve_revision)"
  local created="${DOCKER_CREATED:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

  local args=(
    --load
    --build-arg "VERSION=${version}"
    --build-arg "VCS_REF=${revision}"
    --label "org.opencontainers.image.created=${created}"
    --tag "${image}"
  )
  if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
    args+=(--platform "${DOCKER_PLATFORM}")
  fi
  if [[ -n "${FBN_UID:-}" ]]; then
    args+=(--build-arg "FBN_UID=${FBN_UID}")
  fi
  if [[ -n "${FBN_GID:-}" ]]; then
    args+=(--build-arg "FBN_GID=${FBN_GID}")
  fi
  if [[ "${DOCKER_NO_CACHE:-0}" == "1" ]]; then
    args+=(--pull --no-cache)
  fi

  echo "Building ${image} (version=${version}, revision=${revision:0:12})..."
  docker buildx build "${args[@]}" "${REPO_ROOT}"
}

cmd_pull() {
  load_dotenv_defaults
  local image="${DOCKER_IMAGE:-${FBN_IMAGE:-${DEFAULT_REMOTE_IMAGE}}}"
  echo "Pulling ${image}..."
  docker pull "${image}"
}

smoke_cleanup() {
  local log_container="$1" compose_project="$2" persistence_volume="$3"
  docker rm -f "${log_container}" >/dev/null 2>&1 || true
  FBN_TEST_IMAGE="fbn:cleanup" docker compose --env-file /dev/null --project-name "${compose_project}" -f "${REPO_ROOT}/tests/fixtures/compose_exit.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker volume rm -f "${persistence_volume}" >/dev/null 2>&1 || true
}

cmd_smoke() {
  local image="${DOCKER_IMAGE:-${DEFAULT_IMAGE}}"
  if [[ "${image}" == "${DEFAULT_IMAGE}" ]]; then
    cmd_build
  elif ! docker image inspect "${image}" >/dev/null 2>&1; then
    docker pull "${image}"
  fi

  local expected_revision="${DOCKER_VCS_REF:-}"
  local expected_version="${DOCKER_VERSION:-}"
  if [[ -z "${expected_version}" ]]; then
    expected_version="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "${image}")"
  fi
  if [[ -z "${expected_version}" ]]; then
    expected_version="$(resolve_base_version)"
  fi
  local expected_arch="${EXPECTED_ARCH:-}"

  local run_id="$$-${RANDOM}"
  local log_container="fbn-smoke-log-${run_id}"
  local compose_project="fbn-smoke-exit-${run_id}"
  local persistence_volume="fbn-smoke-persistence-${run_id}"
  local repo_host
  repo_host="$(host_path "${REPO_ROOT}")"

  trap "smoke_cleanup $(printf '%q ' "${log_container}" "${compose_project}" "${persistence_volume}")" EXIT

  echo "Smoke-testing ${image} (version=${expected_version})..."

  local actual_version
  actual_version="$(docker run --rm --network none "${image}" --version)"
  expect_equal "CLI version" "${actual_version}" "fbn, version ${expected_version}"
  docker run --rm --network none "${image}" bootstrap --help >/dev/null
  docker run --rm --network none "${image}" --help >/dev/null

  docker create --name "${log_container}" --network none --entrypoint python "${image}" \
    -c 'from fbn.cli import _configure_logging; from fbn.logging import get_logger; _configure_logging(True); get_logger("monitor").info("Container logging probe", probe=True)' >/dev/null
  docker start "${log_container}" >/dev/null
  docker wait "${log_container}" >/dev/null
  local log_output
  log_output="$(docker logs "${log_container}" 2>&1)"
  grep -F 'Container logging probe | probe=yes' <<<"${log_output}" >/dev/null
  docker rm -f "${log_container}" >/dev/null

  docker image inspect "${image}" |
    docker run --rm -i --network none \
      --env EXPECTED_VERSION="${expected_version}" \
      --env EXPECTED_REVISION="${expected_revision}" \
      --entrypoint python "${image}" -c '
import json, os, sys
image = json.load(sys.stdin)[0]
config = image["Config"]
labels = config.get("Labels") or {}
assert config["User"] == "fbn:fbn", config.get("User")
assert config["Healthcheck"]["Test"] == ["CMD", "python", "-m", "fbn.health"]
if os.environ["EXPECTED_VERSION"]:
    assert labels.get("org.opencontainers.image.version") == os.environ["EXPECTED_VERSION"], labels
if os.environ["EXPECTED_REVISION"]:
    assert labels.get("org.opencontainers.image.revision") == os.environ["EXPECTED_REVISION"], labels
values = "\n".join(config.get("Env", []) + [str(labels)])
assert "FBN_APPRISE_URL" not in values
assert "FBN_AUTH_FILE" not in values
'

  docker history --no-trunc --format '{{.CreatedBy}}' "${image}" |
    docker run --rm -i --network none --entrypoint python "${image}" -c '
import sys
history = sys.stdin.read()
assert "FBN_APPRISE_URL" not in history
assert "facebook-auth.json" not in history
'

  docker run --rm --network none --entrypoint sh "${image}" -ec '
    test "$(id -u)" -ne 0
    if [ -n "$1" ]; then
      test "$(uname -m)" = "$1"
    fi
    test -w /home/fbn/.local/share/fbn
    test ! -e /run/secrets/facebook_auth
  ' sh "${expected_arch}"

  docker run --rm --network none "${image}" doctor --browser chromium >/dev/null

  local expected actual
  for expected in 8 5 1; do
    set +e
    FBN_TEST_IMAGE="${image}" FBN_TEST_EXIT="${expected}" \
      docker compose --env-file /dev/null --project-name "${compose_project}" -f "${REPO_ROOT}/tests/fixtures/compose_exit.yaml" \
      up --no-build --pull never --no-deps --exit-code-from fbn --no-color fbn >/dev/null 2>&1
    actual="$?"
    set -e
    expect_equal "compose exit code (${expected})" "${actual}" "${expected}"
  done
  FBN_TEST_IMAGE="${image}" docker compose --env-file /dev/null --project-name "${compose_project}" -f "${REPO_ROOT}/tests/fixtures/compose_exit.yaml" down --volumes --remove-orphans >/dev/null 2>&1

  docker run --rm \
    --entrypoint python \
    --network none \
    --shm-size 1gb \
    --volume "${repo_host}/tests/mobile_fixtures.py:/fixtures/mobile_fixtures.py:ro" \
    --env PYTHONPATH=/fixtures \
    "${image}" \
    -c '
from playwright.sync_api import sync_playwright
from fbn.mobile import MOBILE_SCAN_SCRIPT, wait_for_mobile_page
from mobile_fixtures import mobile_fixture
runtime = sync_playwright().start()
browser = runtime.chromium.launch(channel="chromium", headless=True)
page = browser.new_page()
page.set_content(mobile_fixture([("101", "Synthetic Author", "Synthetic body")]))
wait_for_mobile_page(page, timeout_seconds=2)
assert page.evaluate(MOBILE_SCAN_SCRIPT, False)["postCount"] == 1
browser.close()
runtime.stop()
'

  docker run --rm \
    --network none \
    --shm-size 1gb \
    --volume "${repo_host}/tests/mobile_fixtures.py:/fixtures/mobile_fixtures.py:ro" \
    --volume "${repo_host}/tests/container_failure_screenshot_smoke.py:/fixtures/screenshot_smoke.py:ro" \
    --entrypoint python \
    "${image}" \
    /fixtures/screenshot_smoke.py >/dev/null

  docker volume create "${persistence_volume}" >/dev/null
  docker run --rm \
    --network none \
    --shm-size 1gb \
    --volume "${persistence_volume}:/home/fbn/.local/share/fbn" \
    --volume "${repo_host}/tests/container_persistence_smoke.py:/smoke.py:ro" \
    --entrypoint python \
    "${image}" \
    /smoke.py write
  docker run --rm \
    --network none \
    --shm-size 1gb \
    --volume "${persistence_volume}:/home/fbn/.local/share/fbn" \
    --volume "${repo_host}/tests/container_persistence_smoke.py:/smoke.py:ro" \
    --entrypoint python \
    "${image}" \
    /smoke.py read
  docker volume rm -f "${persistence_volume}" >/dev/null

  smoke_cleanup "${log_container}" "${compose_project}" "${persistence_volume}"
  trap - EXIT
  echo "Smoke test passed: ${image}"
}

test_cleanup() {
  local test_image="$1"
  docker image rm -f "${test_image}" >/dev/null 2>&1 || true
}

cmd_test() {
  local base_image="${DOCKER_IMAGE:-${DEFAULT_IMAGE}}"
  if [[ "${base_image}" == "${DEFAULT_IMAGE}" ]]; then
    cmd_build
  elif ! docker image inspect "${base_image}" >/dev/null 2>&1; then
    docker pull "${base_image}"
  fi

  local test_image="fbn-test:$$-${RANDOM}"
  local repo_host
  repo_host="$(host_path "${REPO_ROOT}")"
  local test_cmd="${TEST_CMD:-make lint test-unit build check-dist smoke-wheel test-browser}"

  trap "test_cleanup $(printf '%q' "${test_image}")" EXIT

  echo "Building disposable test image ${test_image} from ${base_image}..."
  docker buildx build --load --tag "${test_image}" -f - "${REPO_ROOT}" <<EOF
FROM ${base_image}
USER 0:0
RUN apt-get update \\
    && apt-get install --yes --no-install-recommends git make \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY fbn/ fbn/
RUN mkdir -p /opt/fbn/wheelhouse \\
    && python -m pip download --dest /opt/fbn/wheelhouse "setuptools>=77" wheel ".[dev]" \\
    && python -m pip install --no-index --find-links=/opt/fbn/wheelhouse --no-build-isolation -e ".[dev]" \\
    && python -m pip check \\
    && chmod -R a+rX /opt/fbn/wheelhouse
ENV PIP_FIND_LINKS=/opt/fbn/wheelhouse \\
    PIP_NO_INDEX=1
USER fbn:fbn
WORKDIR /home/fbn
ENTRYPOINT []
EOF

  echo "Running '${test_cmd}' inside isolated --network none container..."
  docker run --rm \
    --network none \
    --shm-size 1gb \
    --volume "${repo_host}:/src:ro" \
    --env TEST_CMD="${test_cmd}" \
    "${test_image}" \
    bash -ec '
      mkdir -p /tmp/workspace
      cp -a /src/. /tmp/workspace/
      rm -rf /tmp/workspace/.env /tmp/workspace/.env.* /tmp/workspace/build /tmp/workspace/dist /tmp/workspace/.pytest_cache /tmp/workspace/.ruff_cache /tmp/workspace/htmlcov /tmp/workspace/.coverage
      cd /tmp/workspace
      git config --global --add safe.directory /tmp/workspace
      python -m pip install --no-deps --no-build-isolation -e . >/dev/null
      eval "${TEST_CMD}"
    '

  test_cleanup "${test_image}"
  trap - EXIT
  echo "Disposable container test run passed."
}

matrix_cleanup() {
  local img
  for img in "$@"; do
    docker image rm -f "${img}" >/dev/null 2>&1 || true
  done
}

cmd_test_matrix() {
  local versions="${PYTHON_VERSIONS:-3.10 3.11 3.12 3.13 3.14}"
  local repo_host
  repo_host="$(host_path "${REPO_ROOT}")"
  local run_id="$$-${RANDOM}"
  local created_images=()
  local pyver
  for pyver in ${versions}; do
    created_images+=("fbn-unit-py${pyver}:${run_id}")
  done

  trap "matrix_cleanup $(printf '%q ' "${created_images[@]}")" EXIT

  for pyver in ${versions}; do
    local unit_image="fbn-unit-py${pyver}:${run_id}"
    echo "=== Python ${pyver}: building disposable test container ${unit_image} ==="
    docker buildx build --load --tag "${unit_image}" -f - "${REPO_ROOT}" <<EOF
FROM python:${pyver}-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PATH=/opt/fbn/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN groupadd --gid 1000 fbn \\
    && useradd --create-home --gid fbn --uid 1000 --home-dir /home/fbn fbn \\
    && python -m venv /opt/fbn/venv \\
    && /opt/fbn/venv/bin/python -m pip install --upgrade pip setuptools wheel
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY fbn/ fbn/
RUN /opt/fbn/venv/bin/python -m pip install -e ".[dev]" \\
    && chown -R fbn:fbn /build /opt/fbn/venv
USER fbn:fbn
WORKDIR /home/fbn
EOF

    echo "=== Python ${pyver}: running pip check and unit tests (--network none) ==="
    docker run --rm \
      --network none \
      --volume "${repo_host}:/src:ro" \
      "${unit_image}" \
      bash -ec '
        mkdir -p /tmp/workspace
        cp -a /src/. /tmp/workspace/
        rm -rf /tmp/workspace/.env /tmp/workspace/.env.* /tmp/workspace/build /tmp/workspace/dist /tmp/workspace/.pytest_cache /tmp/workspace/.ruff_cache /tmp/workspace/htmlcov /tmp/workspace/.coverage
        cd /tmp/workspace
        python -m pip install --no-deps --no-build-isolation -e . >/dev/null
        python -m pip check
        python -m pytest --ignore=tests/test_browser_integration.py --cov=fbn --cov-report=term-missing
      '
    docker image rm -f "${unit_image}" >/dev/null 2>&1 || true
  done

  trap - EXIT
  echo "All Python matrix unit tests (${versions}) passed."
}

cmd_doctor() {
  load_dotenv_defaults
  local image
  image="$(ensure_runtime_image)"
  docker run --rm --network none "${image}" doctor --browser chromium
}

require_env_vars() {
  local missing=()
  local var
  for var in "$@"; do
    if [[ -z "${!var:-}" ]]; then
      missing+=("${var}")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    echo "ERROR: Missing required configuration: ${missing[*]}" >&2
    echo "Set these variables in ${REPO_ROOT}/.env or on the command line." >&2
    echo "See README.md ('Quick start with Docker') for an example .env file." >&2
    return 1
  fi
}

cmd_bootstrap() {
  load_dotenv_defaults
  require_env_vars FBN_GROUP FBN_AUTH_FILE
  if [[ ! -f "${FBN_AUTH_FILE}" ]]; then
    echo "ERROR: FBN_AUTH_FILE does not exist or is not a regular file." >&2
    return 1
  fi
  ensure_data_volume
  local image
  image="$(ensure_runtime_image)"
  echo "Bootstrapping Facebook group session in Docker..."
  FBN_IMAGE="${image}" docker compose --file "${REPO_ROOT}/compose.yaml" run --rm bootstrap
}

cmd_check() {
  load_dotenv_defaults
  require_env_vars FBN_GROUP
  local dry_run="${DRY_RUN:-0}"
  if [[ "${dry_run}" != "1" ]]; then
    require_env_vars FBN_APPRISE_URL
  fi
  ensure_data_volume
  local image
  image="$(ensure_runtime_image)"
  local volume="${FBN_DATA_VOLUME:-fbn-monitor-data}"
  local check_args=(
    check
    --id "${FBN_GROUP}"
    --browser chromium
    --headless
    --verbose
  )
  if [[ "${dry_run}" == "1" ]]; then
    check_args+=(--dry-run)
  fi
  if [[ -n "${DOCKER_CHECK_ARGS:-}" ]]; then
    # shellcheck disable=SC2206
    check_args+=(${DOCKER_CHECK_ARGS})
  fi
  docker run --rm \
    --init \
    --shm-size 1gb \
    --memory 1g \
    --memory-swap 1g \
    --env FBN_APPRISE_URL="${FBN_APPRISE_URL:-}" \
    --env FBN_PROFILE_DIR=/home/fbn/.local/share/fbn/profile \
    --env FBN_STATE_FILE=/home/fbn/.local/share/fbn/state.sqlite3 \
    --env FBN_TIMEZONE="${FBN_TIMEZONE:-UTC}" \
    --volume "${volume}:/home/fbn/.local/share/fbn" \
    "${image}" \
    "${check_args[@]}"
}

cmd_up() {
  load_dotenv_defaults
  require_env_vars FBN_GROUP FBN_APPRISE_URL FBN_AUTH_FILE
  if [[ ! -f "${FBN_AUTH_FILE}" ]]; then
    echo "ERROR: FBN_AUTH_FILE does not exist or is not a regular file." >&2
    return 1
  fi
  ensure_data_volume
  local image
  image="$(ensure_runtime_image)"
  echo "Starting fbn bootstrap and monitor in Docker Compose..."
  # shellcheck disable=SC2086
  FBN_IMAGE="${image}" docker compose --file "${REPO_ROOT}/compose.yaml" up --detach ${DOCKER_UP_ARGS:-}
  echo "fbn is starting. Run 'make docker-logs' to follow live output or 'make docker-down' to stop."
}

cmd_down() {
  load_dotenv_defaults
  docker compose --file "${REPO_ROOT}/compose.yaml" down
}

cmd_logs() {
  load_dotenv_defaults
  docker compose --file "${REPO_ROOT}/compose.yaml" logs --follow
}

cmd_status() {
  load_dotenv_defaults
  docker compose --file "${REPO_ROOT}/compose.yaml" ps
}

cmd_push() {
  local repo="${DOCKER_REPO:-${DEFAULT_REPO}}"
  local tags="${TAGS:?TAGS is required, for example TAGS=\"0.2.0 v0.2.0 latest\"}"
  if [[ "${ALLOW_DIRTY:-0}" != "1" && -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
    echo "ERROR: docker-push publishes the committed revision; commit or remove local changes first." >&2
    git -C "${REPO_ROOT}" status --short >&2
    return 1
  fi
  local version
  version="$(resolve_version)"
  local revision
  revision="$(resolve_revision)"
  local created="${DOCKER_CREATED:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  local platforms="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"

  DOCKER_IMAGE="${DEFAULT_IMAGE}" \
  DOCKER_VERSION="${version}" \
  DOCKER_VCS_REF="${revision}" \
  DOCKER_CREATED="${created}" \
    cmd_smoke

  ghcr_login

  local tag_args=()
  local tag
  for tag in ${tags}; do
    tag_args+=(--tag "${repo}:${tag}")
  done

  echo "Building and pushing ${repo} (${tags}) for ${platforms}..."
  docker buildx build \
    --platform "${platforms}" \
    --build-arg "VERSION=${version}" \
    --build-arg "VCS_REF=${revision}" \
    --label "org.opencontainers.image.created=${created}" \
    "${tag_args[@]}" \
    --push \
    "${REPO_ROOT}"
}

cmd_push_digest() {
  local repo="${DOCKER_REPO:-${DEFAULT_REPO}}"
  local version
  version="$(resolve_version)"
  local revision
  revision="$(resolve_revision)"
  local created="${DOCKER_CREATED:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  local platform="${DOCKER_PLATFORM:?DOCKER_PLATFORM is required}"
  local digests_dir="${DIGESTS_DIR:?DIGESTS_DIR is required}"

  local metadata_file
  metadata_file="$(mktemp)"
  trap "rm -f -- $(printf '%q' "${metadata_file}")" EXIT

  local cache_args=()
  if [[ "${DOCKER_NO_CACHE:-0}" == "1" ]]; then
    cache_args+=(--pull --no-cache)
  fi

  echo "Pushing ${platform} image by digest to ${repo}..."
  docker buildx build \
    --platform "${platform}" \
    --build-arg "VERSION=${version}" \
    --build-arg "VCS_REF=${revision}" \
    --label "org.opencontainers.image.created=${created}" \
    ${cache_args[@]+"${cache_args[@]}"} \
    --output "type=image,name=${repo},push-by-digest=true,name-canonical=true,push=true" \
    --metadata-file "${metadata_file}" \
    "${REPO_ROOT}"

  local digest
  digest="$(jq -r '."containerimage.digest"' "${metadata_file}")"
  test -n "${digest}" && test "${digest}" != "null"
  mkdir -p "${digests_dir}"
  touch "${digests_dir}/${digest#sha256:}"
  rm -f -- "${metadata_file}"
  trap - EXIT
  echo "Pushed ${repo}@${digest}"
}

cmd_publish_manifest() {
  local repo="${DOCKER_REPO:-${DEFAULT_REPO}}"
  local digests_dir="${DIGESTS_DIR:?DIGESTS_DIR is required}"
  local tags="${TAGS:?TAGS is required}"
  local primary_tag="${PRIMARY_TAG:?PRIMARY_TAG is required}"

  local sources=()
  local digest
  for digest in "${digests_dir}"/*; do
    sources+=("${repo}@sha256:$(basename "${digest}")")
  done
  test "${#sources[@]}" -eq 2

  local tag_args=()
  local tag
  for tag in ${tags}; do
    tag_args+=(--tag "${repo}:${tag}")
  done

  docker buildx imagetools create "${tag_args[@]}" "${sources[@]}"
  docker buildx imagetools inspect "${repo}:${primary_tag}" --raw \
    | jq -e '[.manifests[].platform | select(.os == "linux") | .architecture] | sort == ["amd64", "arm64"]' \
      >/dev/null
  echo "Published multi-arch manifest for ${repo} (${tags})"
}

command_name="${1:-}"
case "${command_name}" in
  config)
    cmd_config
    ;;
  build)
    cmd_build
    ;;
  pull)
    cmd_pull
    ;;
  smoke)
    cmd_smoke
    ;;
  test)
    cmd_test
    ;;
  test-matrix)
    cmd_test_matrix
    ;;
  doctor)
    cmd_doctor
    ;;
  bootstrap)
    cmd_bootstrap
    ;;
  check)
    cmd_check
    ;;
  up|run)
    cmd_up
    ;;
  down)
    cmd_down
    ;;
  logs)
    cmd_logs
    ;;
  status)
    cmd_status
    ;;
  push)
    cmd_push
    ;;
  push-digest)
    cmd_push_digest
    ;;
  publish-manifest)
    cmd_publish_manifest
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
