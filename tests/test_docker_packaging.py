from __future__ import annotations

import re
from pathlib import Path

import fbn

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_defines_version_revision_labels_and_nonroot_runtime() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert f"ARG VERSION={fbn.__version__}" in dockerfile
    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'org.opencontainers.image.title="fbn"' in dockerfile
    assert 'org.opencontainers.image.source="https://github.com/viseshrp/fbn"' in (
        dockerfile
    )
    assert 'org.opencontainers.image.version="${VERSION}"' in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "USER fbn:fbn" in dockerfile
    assert 'CMD ["python", "-m", "fbn.health"]' in dockerfile


def test_compose_uses_ghcr_default_image_and_preserves_secret_boundaries() -> None:
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    legacy_link = REPO_ROOT / "docker-compose.yml"

    assert legacy_link.is_symlink()
    assert legacy_link.readlink() == Path("compose.yaml")
    assert compose.count('image: "${FBN_IMAGE:-ghcr.io/viseshrp/fbn:latest}"') == 2
    assert 'VERSION: "${FBN_VERSION:-0.2.0}"' in compose
    assert 'VCS_REF: "${FBN_VCS_REF:-unknown}"' in compose
    assert "/run/secrets/facebook_auth" in compose


def test_makefile_exposes_run_validation_and_ci_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-zA-Z0-9_.-]+):", makefile, flags=re.MULTILINE))

    expected_targets = {
        "help",
        "version",
        "check-version",
        "install",
        "install-dev",
        "install-browser",
        "check-deps",
        "lint",
        "check",
        "test",
        "test-unit",
        "test-browser",
        "build",
        "check-dist",
        "smoke-wheel",
        "smoketest",
        "clean",
        "docker-config",
        "docker-build",
        "docker-pull",
        "docker-smoke",
        "docker-test",
        "docker-test-matrix",
        "docker-doctor",
        "docker-bootstrap",
        "docker-check",
        "docker-check-dry",
        "docker-up",
        "docker-run",
        "up",
        "docker-down",
        "down",
        "docker-logs",
        "logs",
        "docker-status",
        "status",
        "docker-push",
        "docker-push-digest",
        "docker-publish-manifest",
    }
    assert expected_targets.issubset(targets)


def test_ci_workflow_uses_makefile_targets() -> None:
    ci_workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for command in (
        "make install-dev",
        "make check-deps",
        "make test-unit",
        "make lint",
        "make build",
        "make check-dist",
        "make smoke-wheel",
        "make install-browser",
        "make test-browser",
        "make docker-config",
        "make docker-build",
        "make docker-smoke",
    ):
        assert command in ci_workflow


def test_container_workflow_publishes_nightly_and_tagged_ghcr_images() -> None:
    docker_workflow = (REPO_ROOT / ".github/workflows/docker.yml").read_text(
        encoding="utf-8"
    )

    assert "IMAGE: ghcr.io/viseshrp/fbn" in docker_workflow
    assert "schedule:" in docker_workflow
    assert "workflow_dispatch:" in docker_workflow
    assert 'tags:\n      - "v*"' in docker_workflow
    assert "release:\n    types: [published]" in docker_workflow
    assert "secrets.GHCR_TOKEN || secrets.GITHUB_TOKEN" in docker_workflow
    assert "make check-version" in docker_workflow
    assert "make docker-config" in docker_workflow
    assert "make docker-push-digest" in docker_workflow
    assert "make docker-smoke" in docker_workflow
    assert "make docker-publish-manifest" in docker_workflow


def test_smoke_test_leaves_no_anonymous_volume_behind() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    script = (REPO_ROOT / "scripts/docker.sh").read_text(encoding="utf-8")
    fixture = (REPO_ROOT / "tests/fixtures/compose_exit.yaml").read_text(
        encoding="utf-8"
    )

    # The image declares its data directory as a volume, so a container made
    # from it owns an anonymous volume unless something is mounted there.
    (data_dir,) = re.findall(r'^VOLUME \["([^"]+)"\]$', dockerfile, flags=re.MULTILINE)

    # Compose keeps that volume once it has recreated the container, and
    # `down --volumes` then leaves it behind.
    assert f"    tmpfs:\n      - {data_dir}\n" in fixture
    # Only --rm removes a container's anonymous volume with it; a container
    # created without it must be removed together with its volume.
    assert re.findall(r"docker run (?!--rm\b)", script) == []
    removals = re.findall(r"docker rm ([^\n]*)", script)
    assert len(removals) == 2
    assert all(removal.startswith("-f -v ") for removal in removals)
    # The smoke test checks the volumes its own containers mounted.
    assert "expect_volumes_removed ${used_volumes}" in script


def test_agents_and_readme_document_docker_preferred_workflow() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Running the app via Docker is the most preferred way" in agents
    assert "make docker-up" in agents
    assert "Quick start with Docker" in readme
    assert "ghcr.io/viseshrp/fbn:latest" in readme
    assert "ghcr.io/viseshrp/fbn:nightly" in readme
    assert "make docker-up" in readme
    assert "make docker-logs" in readme
    assert "make docker-down" in readme
