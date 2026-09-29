FROM python:3.11-slim

WORKDIR /code

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

ENV PYTHONPATH=/code \
    PYTHONUNBUFFERED=1

# The gateway. `docker compose run --rm backend <cmd>` still runs one-off
# commands (ingest, pytest) in the same image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
