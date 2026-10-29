#!/usr/bin/env bash
# Prepara l'elaborazione locale dei vocali: tutto resta sul PC.
#
#   1. librerie Python della trascrizione, per la scheda video presente: NVIDIA (CUDA),
#      AMD (ROCm) o nessuna (CPU). Dopo un cambio di scheda basta rilanciare lo script.
#   2. Ollama, il motore dei modelli di analisi, solo su localhost, impostato per
#      usare poca memoria (contesto compresso, un modello alla volta)
#   3. scelta del modello di analisi: il migliore che sta tutto nella scheda video
#   4. download dei modelli (una volta sola: poi l'elaborazione funziona offline)
#   5. riconoscimento di chi parla (pyannote, in un ambiente a parte)
#   6. riavvio del worker e diagnosi completa
#
# Ripetibile: i passaggi già fatti vengono saltati.
# Per tenere un modello scelto a mano: OLLAMA_MODEL=... e OLLAMA_MODEL_FISSO=1 in .env
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/prepara-ai.sh  come utente normale."
  echo "La password di amministratore viene chiesta solo nei passaggi che la richiedono."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
[[ -f .env && -x .venv/bin/python ]] || { echo "Prima esegui scripts/setup.sh"; exit 1; }

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }

env_has() { grep -qE "^$1=" .env; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
env_set() {  # aggiunge una chiave a .env solo se manca: le scelte già fatte non si toccano
  env_has "$1" || printf '%s=%s\n' "$1" "$2" >> .env
}
worker_attivo() {
  systemctl --user list-unit-files diario-worker.service >/dev/null 2>&1 \
    && systemctl --user is-enabled --quiet diario-worker 2>/dev/null
}

# shellcheck source=scripts/_gpu.sh
source scripts/_gpu.sh

say "1/6  Librerie per la trascrizione"
"$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r backend/requirements.txt
GPU=$(gpu_descrizione)
[[ -n "$GPU" ]] && echo "   Scheda video: $GPU"
installa_ctranslate2 || warn "la trascrizione userà la CPU finché il problema qui sopra non è risolto"
ok "faster-whisper pronto"

say "2/6  Ollama"
if ! command -v ollama >/dev/null; then
  echo "   Ollama non è installato: lo installo con lo script ufficiale (https://ollama.com/install.sh)."
  echo "   Ti verrà chiesta la password di amministratore."
  curl -fsSL https://ollama.com/install.sh | sh
fi
ollama_up() { curl -fsS --max-time 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; }
if ! ollama_up && command -v systemctl >/dev/null; then
  sudo systemctl enable --now ollama >/dev/null 2>&1 || true
fi
# Meno memoria: memoria del contesto compressa (q8_0, circa la metà); nella scheda video
# solo il modello di analisi (l'embedding gira sulla CPU, nella RAM); solo su localhost.
DROPIN=/etc/systemd/system/ollama.service.d/diario.conf
read -r -d '' CONF <<'EOF' || true
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF
if command -v systemctl >/dev/null && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
  if [[ "$(cat "$DROPIN" 2>/dev/null)" != "$CONF" ]]; then
    echo "   Imposto Ollama per usare meno memoria (password di amministratore)…"
    sudo mkdir -p "$(dirname "$DROPIN")"
    printf '%s\n' "$CONF" | sudo tee "$DROPIN" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl restart ollama
  fi
  ok "contesto compresso, scheda video al solo modello di analisi"
else
  warn "Ollama non gira come servizio di sistema: imposta a mano OLLAMA_KV_CACHE_TYPE=q8_0 e OLLAMA_FLASH_ATTENTION=1"
fi
for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
ollama_up || die "Ollama non risponde su 127.0.0.1:11434. Controlla: systemctl status ollama"
ok "Ollama $(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) in ascolto solo su localhost"

say "3/6  Scelta del modello di analisi"
env_has OLLAMA_EMBED_MODEL || {
  echo "" >> .env
  echo "# --- Elaborazione locale (scritto da scripts/prepara-ai.sh, modificabile) ---" >> .env
}
env_set OLLAMA_EMBED_MODEL "bge-m3"
if [[ "$(env_get OLLAMA_MODEL_FISSO)" == "1" ]] && env_has OLLAMA_MODEL; then
  ok "scelto a mano in .env: $(env_get OLLAMA_MODEL)"
else
  # La prova va fatta con la scheda video libera: il worker si ferma per un momento.
  worker_attivo && systemctl --user stop diario-worker || true
  # shellcheck source=scripts/_cuda.sh
  source scripts/_cuda.sh
  (cd backend && "$ROOT/.venv/bin/python" -m app.cli scegli-modello) || warn "scelta non riuscita: resta $(env_get OLLAMA_MODEL)"
fi

say "4/6  Download dei modelli (solo la prima volta, alcuni GB)"
# shellcheck source=scripts/_cuda.sh
source scripts/_cuda.sh
(cd backend && "$ROOT/.venv/bin/python" -m app.cli prepara-modelli)

say "5/6  Riconoscimento di chi parla"
VOCI="$ROOT/.venv-voci"
MODELLO_VOCI="$ROOT/data/modelli/pyannote-community-1"
PY=$(command -v python3)
if [[ ! -x "$VOCI/bin/python" ]] || ! "$VOCI/bin/python" -c "import pyannote.audio, torch" >/dev/null 2>&1; then
  # torch per la scheda presente (CUDA, ROCm o CPU): con ROCm sono circa 16 GB, con CUDA 5–8
  torch_scelta
  echo "   Preparo un ambiente a parte con pyannote e torch per $TORCH_MOTORE (diversi GB, una volta sola)…"
  "$PY" -m venv "$VOCI"
  "$VOCI/bin/pip" install -q --disable-pip-version-check --upgrade pip
  IDX=()
  [[ -n "$TORCH_IDX" ]] && IDX=(--index-url "$TORCH_IDX")
  _gpu_pip "$VOCI/bin/pip" install -q torch torchaudio "${IDX[@]}" \
    || { warn "torch per la scheda video non disponibile: uso la versione per CPU"
         _gpu_pip "$VOCI/bin/pip" install -q torch torchaudio --index-url "$TORCH_CPU_INDEX"; }
  _gpu_pip "$VOCI/bin/pip" install -q "pyannote.audio==4.0.7" numpy
fi
# Anche un ambiente già pronto va riallineato se la scheda è cambiata (es. NVIDIA → AMD)
allinea_torch "$VOCI"
INFO=$("$VOCI/bin/python" backend/voci_pyannote.py --verifica 2>/dev/null | tail -1 || true)
if [[ -z "$INFO" ]]; then
  warn "pyannote non si avvia: rilancia questo script"
elif [[ "$INFO" == *'"errore_gpu": null'* || "$INFO" == *'"cuda": false'* ]]; then
  ok "pyannote pronto ($INFO)"
else
  warn "pyannote pronto ma la scheda video non funziona, userà la CPU: $INFO"
fi

if [[ ! -f "$MODELLO_VOCI/config.yaml" ]]; then
  echo
  echo "   Il modello delle voci si scarica una volta sola da Hugging Face (poi resta sul PC):"
  echo "     1. crea un account gratuito su https://huggingface.co"
  echo "     2. apri https://huggingface.co/pyannote/speaker-diarization-community-1 e accetta le condizioni"
  echo "     3. crea un token di lettura su https://huggingface.co/settings/tokens"
  echo "   Il token serve solo per questo download e non viene salvato."
  read -r -s -p "   Incolla il token (Invio vuoto per saltare): " HF_TOKEN; echo
  if [[ -n "$HF_TOKEN" ]]; then
    mkdir -p "$(dirname "$MODELLO_VOCI")"
    if HF_TOKEN="$HF_TOKEN" "$VOCI/bin/python" backend/voci_pyannote.py --scarica "$MODELLO_VOCI" >/dev/null; then
      ok "modello delle voci scaricato in data/modelli"
    else
      warn "download non riuscito: hai accettato le condizioni del modello? Rilancia lo script per riprovare"
    fi
  else
    warn "saltato: le note restano senza voci finché non rilanci lo script"
  fi
  unset HF_TOKEN
else
  ok "modello delle voci presente"
fi

say "6/6  Riavvio dell'elaborazione e verifica"
if worker_attivo; then
  systemctl --user restart diario-worker
  sleep 5
else
  warn "il worker non è ancora installato come servizio: esegui scripts/installa-servizio.sh"
fi
# shellcheck disable=SC2015
(cd backend && "$ROOT/.venv/bin/python" -m app.cli diagnosi) && ok "elaborazione pronta" \
  || warn "qualcosa non va: leggi i ✗ qui sopra"
echo
echo "Per rianalizzare le note già fatte (senza ritrascrivere):  scripts/rianalizza.sh"
