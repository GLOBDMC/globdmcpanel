FROM python:3.11-slim-bullseye

WORKDIR /app
COPY requirements.txt .

# pip deps → Playwright binary → Playwright sistem kütüphaneleri (tek RUN katmanı)
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install chromium && \
    python -m playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY . .
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
