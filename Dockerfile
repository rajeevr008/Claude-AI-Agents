# Cloud Run job image for the scheduled report runner (dv360-run-scheduled).
FROM python:3.11-slim

# uv for fast, locked installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (better layer caching), then the project.
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
COPY templates ./templates
RUN uv sync --frozen --no-dev || uv sync --no-dev

# The daily Cloud Scheduler trigger invokes this; the runner decides which
# schedule rows are actually due today from their cadence.
ENTRYPOINT ["uv", "run", "dv360-run-scheduled"]
