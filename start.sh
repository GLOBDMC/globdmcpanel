#!/bin/bash
set -e

echo "==> Playwright Chromium kurulumu kontrol ediliyor..."
playwright install chromium --with-deps 2>&1 || {
  echo "==> --with-deps başarısız, deps olmadan deneniyor..."
  playwright install chromium
}
echo "==> Playwright hazır."

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
