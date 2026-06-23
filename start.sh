#!/bin/bash
set -e
cd "$(dirname "$0")/app"
export DATABASE_URL="sqlite+aiosqlite:////$(dirname "$0")/data/gas_prices.db"
exec python3.11 -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8081}" --log-level info
