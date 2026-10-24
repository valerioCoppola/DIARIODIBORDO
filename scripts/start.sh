#!/usr/bin/env bash
# Avvia il servizio in primo piano (Ctrl+C per fermarlo).
# Per l'avvio automatico: scripts/installa-servizio.sh (servizio systemd utente).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
set -a; source .env; set +a
# shellcheck source=scripts/_db.sh
source scripts/_db.sh

# Il database è il container Docker diario-db, con restart: unless-stopped: Docker
# lo riaccende da solo all'avvio del PC. Qui lo si attende soltanto, così il
# servizio non ha bisogno dei permessi su Docker.
if ! db_ready; then
  docker compose up -d db >/dev/null 2>&1 || true   # riesce solo se l'utente può usare Docker
  echo "Attendo il database su 127.0.0.1:${DB_PORT:-54329}..."
  db_wait 120 || { echo "Database non raggiungibile. Controlla con: sudo docker ps -a   (container diario-db)"; exit 1; }
fi

# Schema del database aggiornato (le migrazioni già applicate vengono saltate).
(cd backend && "$ROOT"/.venv/bin/python -m app.cli migrate)

# Dove raggiungerlo: sul PC e, se configurato, dal telefono via Tailscale.
phone_hint() {
  echo "Sul PC: http://localhost:${PORT:-8000}"
  local serve="" name
  command -v tailscale >/dev/null && serve=$(tailscale serve status 2>/dev/null || true)
  if [[ "$serve" == *"127.0.0.1:${PORT:-8000}"* ]]; then
    name=$(tailscale status --json 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null) || name=""
    echo "Dal telefono (con Tailscale attivo): https://${name:-<pc>.<rete>.ts.net}"
  else
    echo "Accesso dal telefono non ancora configurato: esegui scripts/telefono.sh"
  fi
}
phone_hint || true

cd backend
exec "$ROOT"/.venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
