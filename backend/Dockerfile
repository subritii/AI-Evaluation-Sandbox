FROM python:3.11-slim

WORKDIR /code

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

ENV PYTHONPATH=/code \
    PYTHONUNBUFFERED=1

# Placeholder until the FastAPI gateway lands in Task 4.
CMD ["python", "-m", "app.rag.query", "--help"]
