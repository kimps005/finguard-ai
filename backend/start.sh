#!/bin/sh
set -eu

if [ -n "${DART_API_KEY:-}" ] || [ -n "${FSC_SERVICE_KEY:-}" ]; then
    .venv/bin/python -m backend.sync_stock_master
fi

exec .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
