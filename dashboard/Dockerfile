FROM python:3.11-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The env var repeats .streamlit/config.toml so telemetry stays off even if
# the config file is missing (rule 3: no telemetry).
ENV PYTHONPATH=/code \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
