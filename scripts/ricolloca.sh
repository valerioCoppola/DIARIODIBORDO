#!/usr/bin/env bash
# Rifà solo la collocazione degli argomenti nell'albero dei temi: appunti e trascrizioni restano.
#
#   scripts/ricolloca.sh                  tutte le note, dalla più vecchia, con l'albero ricostruito da zero
#                                         (consigliato dopo un aggiornamento della collocazione). I percorsi
#                                         che hai confermato e le tue indicazioni si riapplicano; le schede
#                                         dei temi si riscrivono; ogni tanto l'albero si riordina.
#   scripts/ricolloca.sh --nota 3f2a9c   una nota sola (bastano i primi caratteri dell'id)
#   scripts/ricolloca.sh --riordina      riordina subito l'albero (di solito lo fa il worker ogni tante note)
#
#   --utente NOME  --diario NOME         solo una persona / un diario (con più persone --utente serve)
#
# Il worker lavora una nota alla volta: lo stato si vede nell'archivio.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/ricolloca.sh  come utente normale."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
set -a; source .env; set +a
cd backend
if [[ "${1:-}" == "--riordina" ]]; then
  shift
  exec "$ROOT"/.venv/bin/python -m app.cli riordina "$@"
fi
args=("$@")
if [[ " ${args[*]} " != *" --nota "* && " ${args[*]} " != *" --tutte "* ]]; then args=(--tutte --da-capo "${args[@]}"); fi
exec "$ROOT"/.venv/bin/python -m app.cli ricolloca "${args[@]}"
