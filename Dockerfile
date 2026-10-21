# syntax=docker/dockerfile:1
#
# f1brief in a container (F37). One image for every command (`run`, `bot`, ...): the
# entrypoint is `f1brief`, the working directory /data holds config.toml, .env and state.db.
# No secrets and no state are baked in; Claude Code authenticates from the environment
# (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or ANTHROPIC_API_KEY), see README.
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t f1brief .

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_VERSION=0.11.32
ARG CLAUDE_CODE_VERSION=2.1.305

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Claude Code's npm package installs a native binary for the build platform (postinstall);
# Node is needed for that step only, so it stays in this stage.
FROM node:22-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION
RUN npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && cp "$(npm root --global)/@anthropic-ai/claude-code/bin/claude.exe" /claude \
    && /claude --version

FROM ${PYTHON_IMAGE}

ENV TZ=Europe/Warsaw \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && echo "${TZ}" > /etc/timezone

WORKDIR /app

# Dependencies first, from uv.lock only: this layer is rebuilt only when the lock changes.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# The checked-in seed articles, for `f1brief seed-stories --seeds /app/story_seeds.toml`.
COPY story_seeds.toml /app/story_seeds.toml

COPY --from=claude /claude /usr/local/bin/claude

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin f1brief \
    && mkdir -p /data \
    && chown f1brief:f1brief /data

# F1BRIEF_CONTAINER makes `claude -p` calls fail early with a clear message when no
# credential is set (there is no Keychain login to fall back on in here).
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/f1brief \
    CLAUDE_CONFIG_DIR=/home/f1brief/.claude \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    F1BRIEF_CONTAINER=1

USER f1brief
WORKDIR /data

ENTRYPOINT ["f1brief"]
CMD ["--help"]
