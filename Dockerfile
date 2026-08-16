# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - build a wheel and install it into a self-contained virtualenv.
# Build tools stay in this stage and never reach the runtime image.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies are installed before the source is copied so that editing code
# does not invalidate the (slow) dependency layer.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2 - runtime. No compilers, no build tools, no source tree.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="instagram-username-finder" \
      org.opencontainers.image.description="A responsible, open-source Instagram username availability scanner." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/FadeHack/instagram-username-finder"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    USERNAME_FINDER_STATE_FILE=/app/data/state.json

# Unprivileged runtime user; /app/data is the only writable location needed.
RUN groupadd --system --gid 1001 finder \
    && useradd --system --uid 1001 --gid finder --home-dir /app --no-create-home finder \
    && mkdir -p /app/data \
    && chown -R finder:finder /app

COPY --from=builder --chown=finder:finder /opt/venv /opt/venv

WORKDIR /app
USER finder

VOLUME ["/app/data"]

ENTRYPOINT ["instagram-finder"]
CMD ["--help"]
