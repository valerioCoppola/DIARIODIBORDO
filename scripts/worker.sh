#!/usr/bin/env bash
# Avvia il worker dell'elaborazione in primo piano (Ctrl+C per fermarlo):
# trascrive, analizza e indicizza i vocali in coda.
# Per l'avvio automatico: scripts/installa-servizio.sh (servizio diario-worker).
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/worker.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
set -a; source .env; set +a
# shellcheck source=scripts/_db.sh
source scripts/_db.sh

# Librerie CUDA del venv (scripts/prepara-ai.sh) visibili a ctranslate2.
# shellcheck source=scripts/_cuda.sh
source scripts/_cuda.sh

if ! db_ready; then
  echo "Attendo il database su 127.0.0.1:${DB_PORT:-54329}..."
  db_wait 120 || { echo "Database non raggiungibile. Controlla con: sudo docker ps -a   (container diario-db)"; exit 1; }
fi
(cd backend && "$ROOT"/.venv/bin/python -m app.cli migrate)
cd backend
exec "$ROOT"/.venv/bin/python -m app.worker
