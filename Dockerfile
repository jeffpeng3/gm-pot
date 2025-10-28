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

RUN apt-get install xvfb -y

ENV PATH="/app/.venv/bin:$PATH"

ENV PYTHONUNBUFFERED=1

WORKDIR /app

CMD ["python3", "-u", "main.py"]