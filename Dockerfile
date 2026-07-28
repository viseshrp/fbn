# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04 AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG PLAYWRIGHT_VERSION=1.61.0

ENV PATH=/opt/fbn/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/fbn/venv \
    && python -m pip install --upgrade pip setuptools wheel

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY fbn/ fbn/

RUN python -m pip install "playwright==${PLAYWRIGHT_VERSION}" \
    && python -m pip install . \
    && EXPECTED_PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION}" \
        python -c "import os; from importlib.metadata import version; assert version('playwright') == os.environ['EXPECTED_PLAYWRIGHT_VERSION']"


FROM ubuntu:24.04 AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG PLAYWRIGHT_VERSION=1.61.0
ARG FBN_UID=1000
ARG FBN_GID=1000

ENV HOME=/home/fbn \
    PATH=/opt/fbn/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_DATA_HOME=/home/fbn/.local/share

LABEL org.opencontainers.image.source="https://github.com/viseshrp/fbn" \
      org.opencontainers.image.description="Local Playwright-based Facebook group monitor" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/fbn/venv /opt/fbn/venv

RUN python -m playwright install --with-deps chromium \
    && EXPECTED_PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION}" \
        python -c "import os; from importlib.metadata import version; assert version('playwright') == os.environ['EXPECTED_PLAYWRIGHT_VERSION']" \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    test "${FBN_UID}" -ge 1000; \
    test "${FBN_GID}" -ge 1000; \
    existing_group="$(getent group "${FBN_GID}" | cut -d: -f1 || true)"; \
    if [ -z "${existing_group}" ]; then \
        groupadd --gid "${FBN_GID}" fbn; \
    elif [ "${existing_group}" != "fbn" ]; then \
        groupmod --new-name fbn "${existing_group}"; \
    fi; \
    existing_user="$(getent passwd "${FBN_UID}" | cut -d: -f1 || true)"; \
    if [ -z "${existing_user}" ]; then \
        useradd \
            --create-home \
            --gid fbn \
            --home-dir /home/fbn \
            --shell /usr/sbin/nologin \
            --uid "${FBN_UID}" \
            fbn; \
    elif [ "${existing_user}" != "fbn" ]; then \
        usermod \
            --gid fbn \
            --home /home/fbn \
            --login fbn \
            --move-home \
            --shell /usr/sbin/nologin \
            "${existing_user}"; \
    fi; \
    install \
        --directory \
        --group fbn \
        --mode 0700 \
        --owner fbn \
        /home/fbn/.local/share/fbn

VOLUME ["/home/fbn/.local/share/fbn"]

USER fbn:fbn
WORKDIR /home/fbn

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=5m --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import fbn"]

ENTRYPOINT ["fbn"]
CMD ["--help"]
