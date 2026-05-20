FROM python:3.11-slim-bullseye

# Playwright Chromium sistem bağımlılıkları - manuel kurulum
# (playwright install-deps yerine: mirror sorunlarında --fix-missing çalışır)
RUN apt-get update && apt-get install -y --no-install-recommends --fix-missing \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
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
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libexpat1 \
    libatspi2.0-0 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Playwright binary kur (install-deps YOK - sistem kütüphaneleri üstte kuruldu)
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install chromium

COPY . .
CMD ["python", "start.py"]
