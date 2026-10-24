#!/usr/bin/env bash
# Esegue i test del backend su un database temporaneo creato nel Postgres di Docker.
# Non tocca il database "diario" con i tuoi dati.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
set -a; source .env; set +a
export TEST_DATABASE_ADMIN_URL="postgresql://diario:${DB_ADMIN_PASSWORD}@127.0.0.1:${DB_PORT:-54329}/postgres"
cd backend
exec "$ROOT"/.venv/bin/python -m pytest "$@"
