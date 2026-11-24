#!/bin/sh
set -eu

# Apply the additive migration to both fresh and existing Module 1 volumes.
.venv/bin/python -m backend.dart_etl --migrate

if [ -n "${DART_API_KEY:-}" ] || [ -n "${FSC_SERVICE_KEY:-}" ]; then
    .venv/bin/python -m backend.sync_stock_master
fi

exec .venv/bin/python -m backend.server --host 0.0.0.0 --port 8000
