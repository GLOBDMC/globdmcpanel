FROM python:3.11-slim-bullseye

WORKDIR /app
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install chromium && \
    python -m playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY . .
CMD ["python", "start.py"]
