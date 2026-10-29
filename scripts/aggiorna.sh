#!/usr/bin/env bash
# Applica un aggiornamento del codice: librerie Python, interfaccia, database,
# servizi. Da lanciare ogni volta che i file del progetto cambiano.
#
#   scripts/aggiorna.sh
#
# Ripetibile. Non tocca .env, i dati né i modelli (per quelli: scripts/prepara-ai.sh).
# Prima delle migrazioni salva una copia del database in data/backup/.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/aggiorna.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
[[ -f .env && -x .venv/bin/python ]] || { echo "Prima esegui scripts/setup.sh"; exit 1; }

say() { printf '\n== %s\n' "$*"; }
ok()  { printf '   ✓ %s\n' "$*"; }

say "1/4  Librerie Python"
"$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r backend/requirements-dev.txt
# ctranslate2 per la scheda presente (NVIDIA, AMD o CPU): requirements.txt porta quello di PyPI
# shellcheck source=scripts/_gpu.sh
source scripts/_gpu.sh
installa_ctranslate2 || echo "   La trascrizione userà la CPU: per i dettagli lancia scripts/prepara-ai.sh"
ok "aggiornate"

say "2/4  Interfaccia"
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && NEXT_TELEMETRY_DISABLED=1 npm run build >/dev/null)
ok "compilata in frontend/out"

say "3/4  Database"
set -a; source .env; set +a
# shellcheck source=scripts/_db.sh
source scripts/_db.sh
db_wait 60 || { echo "Database non raggiungibile: sudo docker ps -a (container diario-db)"; exit 1; }
# Prima di cambiare lo schema, una copia del database (i file audio non cambiano).
DOCKER=(docker); docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
mkdir -p data/backup && chmod 700 data/backup
COPIA="data/backup/diario-$(date +%Y%m%d-%H%M%S).sql.gz"
if "${DOCKER[@]}" exec diario-db pg_dump -U diario diario | gzip > "$COPIA" && [[ -s "$COPIA" ]]; then
  chmod 600 "$COPIA"
  ok "copia del database in $COPIA"
else
  rm -f "$COPIA"
  echo "   Copia del database non riuscita: mi fermo prima di toccare lo schema."; exit 1
fi
(cd backend && "$ROOT/.venv/bin/python" -m app.cli migrate)

say "4/4  Servizi"
if systemctl --user is-enabled --quiet diario 2>/dev/null; then
  # Reinstalla i servizi: aggiunge quelli nuovi (es. diario-worker) e riavvia tutto.
  scripts/installa-servizio.sh
else
  echo "   Avvio automatico non installato: avvia a mano con scripts/start.sh e scripts/worker.sh,"
  echo "   oppure installalo con scripts/installa-servizio.sh"
fi
