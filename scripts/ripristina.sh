#!/usr/bin/env bash
# Ripristina una copia fatta con scripts/backup.sh: SOSTITUISCE database e audio attuali.
#
#   scripts/ripristina.sh data/backup/diario-completo-AAAAMMGG-HHMMSS.tar.gz
#
# Su un PC nuovo: prima scripts/setup.sh (crea .env e il database vuoto), poi questo.
# Prima di sostituire, salva comunque una copia di ciò che c'è ora in data/backup/.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/ripristina.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
COPIA="${1:-}"
[[ -f "$COPIA" ]] || { echo "Uso: scripts/ripristina.sh FILE.tar.gz   (creato da scripts/backup.sh)"; exit 1; }
[[ -f .env ]] || { echo "Prima esegui scripts/setup.sh"; exit 1; }
COPIA="$(realpath "$COPIA")"
set -a; source .env; set +a
DB="${DIARIO_DB:-diario}"            # solo per le prove: un altro database
AUDIO="${DIARIO_AUDIO:-$ROOT/data/audio}"
DOCKER=(docker); docker info >/dev/null 2>&1 || DOCKER=(sudo docker)

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$COPIA"
[[ -f "$TMP/database.sql.gz" && -f "$TMP/audio.tar" ]] || { echo "Non è una copia di scripts/backup.sh"; exit 1; }
cat "$TMP/LEGGIMI.txt" 2>/dev/null || true

echo
echo "Attenzione: database e audio attuali verranno sostituiti da quelli della copia."
read -rp "Per continuare scrivi  ripristina : " ok
[[ "$ok" == "ripristina" ]] || { echo "Annullato"; exit 1; }

if [[ "$DB" == diario ]]; then
  systemctl --user stop diario diario-worker 2>/dev/null || true
  echo "Copia di sicurezza di ciò che c'è ora…"
  scripts/backup.sh >/dev/null
fi

echo "Database…"
"${DOCKER[@]}" exec diario-db psql -q -U diario -d postgres \
  -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" -c "CREATE DATABASE \"$DB\""
gunzip -c "$TMP/database.sql.gz" | "${DOCKER[@]}" exec -i diario-db psql -q -U diario -d "$DB" >/dev/null

echo "Audio…"
mkdir -p "$AUDIO"
find "$AUDIO" -mindepth 1 -delete
tar -C "$TMP" -xf "$TMP/audio.tar"
cp -a "$TMP/audio/." "$AUDIO/"
chmod 700 "$AUDIO"

# La password del ruolo dell'applicazione è quella di questo .env.
(cd backend && DATABASE_ADMIN_URL="postgresql://diario:${DB_ADMIN_PASSWORD}@127.0.0.1:${DB_PORT:-54329}/$DB" \
  "$ROOT/.venv/bin/python" -m app.cli migrate)

if [[ "$DB" == diario ]] && systemctl --user is-enabled --quiet diario 2>/dev/null; then
  systemctl --user start diario diario-worker
fi
echo "Ripristino completato."
