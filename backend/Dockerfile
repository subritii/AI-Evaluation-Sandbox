FROM python:3.11-slim AS base

WORKDIR /code

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

ENV PYTHONPATH=/code \
    PYTHONUNBUFFERED=1

# Run as an unprivileged user; the gateway writes nothing to disk.
RUN useradd --create-home --uid 10001 app
USER app

# `tools`: the same environment plus git, for evaluation scripts run inside the
# isolated network (git lets reports record the commit they ran from).
FROM base AS tools
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /repo
USER app

# Default target: the gateway. `docker compose run --rm backend <cmd>` still
# runs one-off commands (ingest, pytest) in the same image.
FROM base AS gateway
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
