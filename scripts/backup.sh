#!/usr/bin/env bash
# Copia completa del Diario: database (persone, diari, note, temi…) e audio originali.
#
#   scripts/backup.sh                 crea data/backup/diario-completo-AAAAMMGG-HHMMSS.tar.gz
#   scripts/backup.sh /media/disco    la crea in un'altra cartella (es. un disco esterno)
#
# Non contiene .env (le password del database) né i modelli: su un altro PC si rifanno con
# scripts/setup.sh e scripts/prepara-ai.sh, poi si ripristina con scripts/ripristina.sh.
# Il file contiene tutti i vocali: tienilo in un posto sicuro (permessi 0600).
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/backup.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
[[ -f .env ]] || { echo "Prima esegui scripts/setup.sh"; exit 1; }
set -a; source .env; set +a
DEST="${1:-$ROOT/data/backup}"
mkdir -p "$DEST"
DOCKER=(docker); docker info >/dev/null 2>&1 || DOCKER=(sudo docker)

STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
umask 077

echo "Database…"
"${DOCKER[@]}" exec diario-db pg_dump -U diario diario | gzip > "$TMP/database.sql.gz"
echo "Audio…"
tar -C "$ROOT/data" -cf "$TMP/audio.tar" audio
printf 'Diario di Bordo, copia del %s\n' "$(date '+%d/%m/%Y %H:%M')" > "$TMP/LEGGIMI.txt"

OUT="$DEST/diario-completo-$STAMP.tar.gz"
tar -C "$TMP" -czf "$OUT" LEGGIMI.txt database.sql.gz audio.tar
chmod 600 "$OUT"
echo "Fatto: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Per ripristinarla: scripts/ripristina.sh \"$OUT\""
