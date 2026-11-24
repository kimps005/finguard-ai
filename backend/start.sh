#!/bin/sh
set -eu

# Apply the additive migration to both fresh and existing Module 1 volumes.
.venv/bin/python -m backend.dart_etl --migrate

if [ -n "${DART_API_KEY:-}" ] && [ -n "${FSC_SERVICE_KEY:-}" ]; then
    .venv/bin/python -m backend.sync_stock_master || echo "stock_master sync failed; continuing" >&2
else
    echo "DART_API_KEY and FSC_SERVICE_KEY are both required to sync stock_master; skipping" >&2
fi

exec .venv/bin/python -m backend.server --host 0.0.0.0 --port 8000
