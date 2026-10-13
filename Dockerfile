# One image, two Railway services: `web` (uvicorn) and `ingest` (hourly cron, runs once and exits).
FROM python:3.12-slim-trixie

# postgresql-client provides pg_dump for the optional daily backup. Trixie ships v17;
# pg_dump must be >= the server's major version (check Railway's Postgres version).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

# Default command is the web service: apply migrations (idempotent), then serve. If migrations
# fail (e.g. DATABASE_URL missing) the app still starts and explains the problem on every page.
# The ingest service overrides this command (railway.cron.json).
CMD ["sh", "-c", "alembic upgrade head || echo \"WARNING: migrations failed, see error above\"; exec uvicorn tracker.web.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
