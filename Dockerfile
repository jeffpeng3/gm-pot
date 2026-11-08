FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

RUN uv run patchright install chromium --with-deps

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH"

ENV PYTHONUNBUFFERED=1
ENV PROFILE_DIR=/app/profile
ENV LOG_DIR=/app/logs
ENV RUNTIME_FILE=/app/state/runtime.json

RUN mkdir -p /app/profile /app/logs /app/state
VOLUME ["/app/profile", "/app/state"]

WORKDIR /app

CMD ["python3", "-u", "main.py"]