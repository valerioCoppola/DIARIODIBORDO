#!/usr/bin/env bash
# Rifà l'analisi delle note partendo dalle trascrizioni salvate (o, con --ritrascrivi, dall'audio).
#
#   scripts/rianalizza.sh                       tutte le note, dalla più vecchia: albero dei temi con le
#                                               schede, appunti e cose fatte ricreati da zero,
#                                               chi parla riconosciuto di nuovo (consigliato dopo un aggiornamento)
#   scripts/rianalizza.sh --nota 3f2a9c        una nota sola (bastano i primi caratteri dell'id,
#                                               quelli dopo ?id= nell'indirizzo della nota)
#   scripts/rianalizza.sh --nota 3f2a9c --voci --persone 4
#                                               riconosce di nuovo le voci sapendo che sono 4 persone
#   scripts/rianalizza.sh --ritrascrivi        tutto come il primo, ma rifà anche la trascrizione dall'audio
#                                               originale (glossario dei nomi, paragrafi nuovi). Le trascrizioni
#                                               corrette a mano restano: --anche-corrette per rifare anche quelle
#
#   --utente NOME  --diario NOME             solo una persona / un diario (con più persone --utente serve)
#
# Ogni diario ha il suo albero dei temi: si ricostruisce diario per diario.
# Le cose da fare spuntate o toccate a mano restano. Il worker lavora una nota alla volta:
# lo stato si vede nell'archivio.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/rianalizza.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
set -a; source .env; set +a
args=("$@")
# Senza --nota né --tutte: tutte le note, da capo (con --utente o --diario si restringe a quelli).
if [[ " ${args[*]} " != *" --nota "* && " ${args[*]} " != *" --tutte "* ]]; then
  if [[ " ${args[*]} " == *" --ritrascrivi "* ]]; then args=(--tutte --da-capo "${args[@]}")
  else args=(--tutte --da-capo --voci "${args[@]}"); fi
fi
cd backend
exec "$ROOT"/.venv/bin/python -m app.cli rianalizza "${args[@]}"
