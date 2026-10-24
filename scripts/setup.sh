#!/usr/bin/env bash
# Prima installazione sul PC: .env con password casuali, database in Docker,
# ambiente Python, migrazioni, build del frontend, utente.
# Ripetibile: i passaggi già fatti vengono saltati.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/setup.sh  come utente normale."
  echo "La password di amministratore viene chiesta solo nei passaggi che la richiedono."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"

say() { printf '\n== %s\n' "$*"; }
ask() { local a; read -rp "$1 [S/n] " a; [[ ! "${a,,}" =~ ^n ]]; }
need() { command -v "$1" >/dev/null || { echo "Manca '$1': $2"; exit 1; }; }

need docker "installa Docker Engine (https://docs.docker.com/engine/install/)"
need python3 "installa Python 3.11 o successivo"
need npm "installa Node.js 20 o successivo"
docker compose version >/dev/null || { echo "Manca il plugin 'docker compose'"; exit 1; }
# Senza permessi diretti su Docker si passa da sudo (chiede la password).
DOCKER=(docker); docker info >/dev/null 2>&1 || DOCKER=(sudo docker)

if [[ ! -f .env ]]; then
  say "Creo .env con password casuali"
  rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
  ADMIN=$(rand); APP=$(rand); PORT=54329
  sed -e "s|^DB_ADMIN_PASSWORD=.*|DB_ADMIN_PASSWORD=$ADMIN|" \
      -e "s|^DB_APP_PASSWORD=.*|DB_APP_PASSWORD=$APP|" \
      -e "s|^DATABASE_ADMIN_URL=.*|DATABASE_ADMIN_URL=postgresql://diario:$ADMIN@127.0.0.1:$PORT/diario|" \
      -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://diario_app:$APP@127.0.0.1:$PORT/diario|" \
      .env.example > .env
  chmod 600 .env
else
  say ".env già presente, lo uso"
fi

mkdir -p data/audio data/tmp/uploads data/postgres
chmod 700 data data/audio data/tmp

say "Avvio il database (Docker)"
"${DOCKER[@]}" compose up -d db
for _ in $(seq 1 60); do
  [[ "$("${DOCKER[@]}" inspect -f '{{.State.Health.Status}}' diario-db 2>/dev/null)" == healthy ]] && break
  sleep 1
done
[[ "$("${DOCKER[@]}" inspect -f '{{.State.Health.Status}}' diario-db)" == healthy ]] || { echo "Il database non è partito: sudo docker compose logs db"; exit 1; }

say "Ambiente Python"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r backend/requirements-dev.txt

say "Migrazioni"
(cd backend && "$ROOT/.venv/bin/python" -m app.cli migrate)

say "Build del frontend"
(cd frontend && npm ci --no-audit --no-fund && NEXT_TELEMETRY_DISABLED=1 npm run build)

if ! "${DOCKER[@]}" exec diario-db psql -U diario -d diario -tAc "SELECT 1 FROM users LIMIT 1" | grep -q 1; then
  say "Crea il tuo utente (amministratore: potrai invitare altre persone)"
  read -rp "Nome utente: " U
  (cd backend && "$ROOT/.venv/bin/python" -m app.cli create-user "$U" --admin)
fi

say "Installazione completata"
if [[ -t 0 ]]; then
  if ask "Preparo l'elaborazione locale (Ollama e modelli di trascrizione e analisi, alcuni GB)?"; then
    scripts/prepara-ai.sh
  else
    echo "Quando vuoi: scripts/prepara-ai.sh"
  fi
  if ask "Avvio automatico all'accensione del PC (consigliato, serve per usarlo da fuori casa)?"; then
    scripts/installa-servizio.sh
  else
    echo "Avvio manuale: scripts/start.sh"
  fi
  if ask "Configuro ora l'accesso dal telefono, ovunque tu sia, con Tailscale?"; then
    scripts/telefono.sh
  else
    echo "Quando vuoi: scripts/telefono.sh"
  fi
else
  echo "Prossimi passi: scripts/prepara-ai.sh (modelli), scripts/installa-servizio.sh (avvio automatico),"
  echo "                scripts/telefono.sh (telefono)"
fi
