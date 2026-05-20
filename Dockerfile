FROM python:3.11-slim-bullseye

# Chromium sistem kütüphaneleri
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    libglib2.0-0 \
    libdbus-1-3 \
    libexpat1 \
    libatspi2.0-0 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libxcomposite1 \
    libxfixes3 \
    libgobject-2.0-0 \
    libgio-2.0-0 \
    fonts-liberation \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium binary'yi build sırasında indir
RUN python -m playwright install chromium

COPY . .

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
