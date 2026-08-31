# syntax=docker/dockerfile:1

# ---- builder: resolve and install dependencies with uv --------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first so this layer caches independently of app code.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Now add the project itself. LICENSE is required because pyproject.toml
# references it via license = { file = "LICENSE" }.
COPY app ./app
COPY templates ./templates
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# ---- runtime -------------------------------------------------------------
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    USVISA_CONFIG=/app/config.yaml

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
COPY --chown=app:app templates ./templates

# State snapshot lives here; mount a volume over it to persist across restarts.
RUN mkdir -p /app/data && chown app:app /app/data

USER app
EXPOSE 8000

ENTRYPOINT ["usvisa"]
