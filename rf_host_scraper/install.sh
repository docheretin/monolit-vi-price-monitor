#!/usr/bin/env bash
# Установка зависимостей скрейпера ВИ на внешнем хосте.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo ">>> Устанавливаю Python-зависимости..."
"$PY" -m pip install -r requirements.txt

echo ">>> Устанавливаю Chromium для Playwright (~150-300 МБ)..."
"$PY" -m playwright install chromium

echo
echo ">>> Готово. Дальше:"
echo "    1) Проверьте, что в файле .env заполнен INGEST_TOKEN (он уже подставлен при доставке)."
echo "    2) Первый запуск с окном браузера, чтобы при необходимости разово пройти капчу:"
echo "         $PY scrape_and_push.py"
echo "    3) Затем настройте cron на ~07:40 по Москве с HEADLESS=1 (см. README_RU.md)."
