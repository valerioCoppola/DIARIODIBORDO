# shellcheck shell=bash
# Scheda video per l'elaborazione: NVIDIA (CUDA), AMD (ROCm) oppure nessuna (CPU).
# Usato da prepara-ai.sh e aggiorna.sh. Richiede ROOT.
#
#   gpu_vendor             nvidia | amd | vuoto (nessuna scheda utilizzabile)
#   gpu_descrizione        es. "AMD Radeon RX 9060 XT, 16304 MB di memoria video"
#   installa_ctranslate2   nel .venv il ctranslate2 giusto per la scheda (trascrizione)
#   torch_scelta           TORCH_IDX e TORCH_MOTORE per l'ambiente delle voci
#   allinea_torch DIR      in DIR (.venv-voci) il torch giusto per la scheda
#
# Cambiare scheda (NVIDIA <-> AMD) e rilanciare prepara-ai.sh rimette tutto a posto.

# ctranslate2 per ROCm non è su PyPI: arriva dalle release del progetto su GitHub,
# nella stessa versione di backend/requirements.txt. Aggiornando ctranslate2, aggiungere
# qui l'impronta del nuovo rocm-python-wheels-Linux.zip (la mostra la pagina della release).
CT2_ROCM_SHA256_4_8_4=6994f777556843b0545b517a1fd51f91836d8c97198aca76c5fa80bc3115b1ac
# torch per ROCm dall'indice ufficiale di PyTorch (comprende le Radeon RX 9000, gfx1200/1201)
TORCH_ROCM_INDEX=https://download.pytorch.org/whl/rocm7.2
TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
# Librerie ROCm di sistema usate da ctranslate2, e i pacchetti Ubuntu/Debian che le portano
ROCM_LIBS=(libamdhip64.so.7 libhipblas.so.3 libhiprand.so.1)
ROCM_APT=(libamdhip64-7 libhipblas3 libhiprand1)
# Le schede integrate nel processore hanno poca memoria riservata: per i modelli serve una dedicata
AMD_VRAM_MIN=$((4 * 1024 * 1024 * 1024))

_gpu_ok()   { printf '   ✓ %s\n' "$*"; }
_gpu_warn() { printf '   ! %s\n' "$*"; }
_gpu_pip()  { TMPDIR="$ROOT/data/tmp/pip" "$@" --disable-pip-version-check; }  # su disco: /tmp può essere in RAM
mkdir -p "$ROOT/data/tmp/pip"

gpu_vendor() {
  if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    echo nvidia
    return
  fi
  local d
  for d in /sys/class/drm/card[0-9]*/device; do
    [[ "$(cat "$d/vendor" 2>/dev/null)" == 0x1002 ]] || continue
    if (( $(cat "$d/mem_info_vram_total" 2>/dev/null || echo 0) >= AMD_VRAM_MIN )); then
      echo amd
      return
    fi
  done
}

gpu_descrizione() {
  (cd "$ROOT/backend" && "$ROOT/.venv/bin/python" -c '
from app.pipeline.gpu import main_gpu
g = main_gpu()
print(f"{g.vendor} {g.name}, {g.vram_mb} MB di memoria video" if g else "")')
}

# Vero se nel .venv c'è il ctranslate2 per ROCm (senza importarlo: senza ROCm non si caricherebbe)
_ct2_rocm() {
  "$ROOT/.venv/bin/python" - <<'EOF' 2>/dev/null
import glob, importlib.util, os, sys
s = importlib.util.find_spec("ctranslate2")
libs = glob.glob(os.path.join(os.path.dirname(os.path.dirname(s.origin)), "ctranslate2.libs", "libctranslate2*")) if s else []
sys.exit(0 if any(b"libamdhip64" in open(f, "rb").read() for f in libs) else 1)
EOF
}

_rimuovi_cuda() {  # pacchetti nvidia-* in un ambiente Python: senza scheda NVIDIA sono solo peso
  local pip=$1 pkgs
  pkgs=$("$pip" list --format=freeze 2>/dev/null | grep -E '^nvidia-' | cut -d= -f1 || true)
  if [[ -n "$pkgs" ]]; then
    # shellcheck disable=SC2086
    "$pip" uninstall -y -q $pkgs >/dev/null 2>&1 || true
  fi
}

# ROCm usa /dev/kfd e /dev/dri/renderD*, del gruppo render. La sessione grafica dà
# l'accesso anche senza gruppo, ma il worker deve funzionare pure senza (servizio, SSH).
_gruppo_render() {
  getent group render >/dev/null || return 0
  id -nG "$USER" | grep -qw render && return 0
  echo "   Aggiungo $USER al gruppo render per usare la scheda video (password di amministratore)…"
  if sudo usermod -aG render "$USER"; then
    _gpu_warn "riavvia il PC quando hai finito: il gruppo nuovo vale dal prossimo avvio"
  else
    _gpu_warn "non riuscito: esegui  sudo usermod -aG render $USER  e riavvia"
  fi
}

_rocm_runtime() {
  local l mancano=() note
  note=$(ldconfig -p 2>/dev/null || true)
  for l in "${ROCM_LIBS[@]}"; do
    grep -qF "$l (" <<<"$note" || [[ -e "/opt/rocm/lib/$l" ]] || mancano+=("$l")
  done
  (( ${#mancano[@]} == 0 )) && return 0
  if command -v apt-get >/dev/null && apt-cache show "${ROCM_APT[@]}" >/dev/null 2>&1; then
    echo "   Installo le librerie ROCm di sistema (${ROCM_APT[*]}): password di amministratore…"
    sudo apt-get install -y -q "${ROCM_APT[@]}" >/dev/null && return 0
  fi
  _gpu_warn "mancano le librerie ROCm ${mancano[*]}: installa ROCm (https://rocm.docs.amd.com) e rilancia"
  return 1
}

_installa_ct2_rocm() {
  local ver=$1 var sha cache zip
  var="CT2_ROCM_SHA256_${ver//./_}"
  sha=${!var:-}
  [[ -n "$sha" ]] || { _gpu_warn "manca l'impronta di ctranslate2 $ver per ROCm in scripts/_gpu.sh"; return 1; }
  cache="$ROOT/data/tmp/gpu"
  zip="$cache/ctranslate2-$ver-rocm-linux.zip"
  mkdir -p "$cache"
  if ! echo "$sha  $zip" | sha256sum -c --quiet >/dev/null 2>&1; then
    echo "   Scarico ctranslate2 $ver per ROCm (circa 370 MB, una volta sola)…"
    curl -fL -C - --retry 10 -sS -o "$zip" \
      "https://github.com/OpenNMT/CTranslate2/releases/download/v$ver/rocm-python-wheels-Linux.zip" \
      || { _gpu_warn "download non riuscito: rilancia lo script (riprende da dove era arrivato)"; return 1; }
    echo "$sha  $zip" | sha256sum -c --quiet >/dev/null 2>&1 \
      || { rm -f "$zip"; _gpu_warn "download corrotto (impronta diversa): rilancia lo script"; return 1; }
  fi
  # Nello zip c'è un pacchetto per ogni versione di Python: si estrae quello del .venv
  local whl
  whl=$("$ROOT/.venv/bin/python" - "$zip" "$cache" "$ver" <<'EOF'
import fnmatch, sys, zipfile
z, dest, ver = sys.argv[1:]
tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
with zipfile.ZipFile(z) as f:
    for n in f.namelist():
        if fnmatch.fnmatch(n.rsplit("/", 1)[-1], f"ctranslate2-{ver}-{tag}-{tag}-manylinux*x86_64*.whl"):
            print(f.extract(n, dest))
            break
EOF
)
  [[ -n "$whl" ]] || { _gpu_warn "nessun ctranslate2 per ROCm adatto a questa versione di Python"; return 1; }
  _gpu_pip "$ROOT/.venv/bin/pip" install -q --force-reinstall --no-deps "$whl"
  rm -rf "$zip" "$(dirname "$whl")"
}

installa_ctranslate2() {
  local pip="$ROOT/.venv/bin/pip" ver
  ver=$(grep -E '^ctranslate2==' "$ROOT/backend/requirements.txt" | cut -d= -f3)
  case "$(gpu_vendor)" in
    nvidia)
      if _ct2_rocm; then _gpu_pip "$pip" install -q --force-reinstall --no-deps "ctranslate2==$ver"; fi
      _gpu_pip "$pip" install -q -r "$ROOT/backend/requirements-gpu.txt"
      _gpu_ok "trascrizione sulla scheda NVIDIA (CUDA)"
      ;;
    amd)
      _rimuovi_cuda "$pip"
      _gruppo_render
      _rocm_runtime || return 1
      _ct2_rocm || _installa_ct2_rocm "$ver" || return 1
      _gpu_ok "trascrizione sulla scheda AMD (ctranslate2 $ver per ROCm)"
      ;;
    *)
      if _ct2_rocm; then _gpu_pip "$pip" install -q --force-reinstall --no-deps "ctranslate2==$ver"; fi
      _rimuovi_cuda "$pip"
      _gpu_warn "nessuna scheda video NVIDIA o AMD utilizzabile: la trascrizione userà la CPU, più lentamente"
      ;;
  esac
}

torch_scelta() {
  TORCH_IDX=$TORCH_CPU_INDEX
  TORCH_MOTORE=CPU
  case "$(gpu_vendor)" in
    amd)
      TORCH_IDX=$TORCH_ROCM_INDEX
      TORCH_MOTORE=ROCm
      ;;
    nvidia)
      # torch con le librerie CUDA adatte al driver; driver troppo vecchio: CPU
      local cv maj min
      cv=$(nvidia-smi | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)
      maj=${cv%%.*}
      min=${cv#*.}
      if   (( ${maj:-0} >= 13 ));               then TORCH_IDX=""; TORCH_MOTORE=CUDA   # PyPI
      elif (( ${maj:-0} == 12 && min >= 8 ));   then TORCH_IDX=https://download.pytorch.org/whl/cu128; TORCH_MOTORE=CUDA
      elif (( ${maj:-0} == 12 && min >= 6 ));   then TORCH_IDX=https://download.pytorch.org/whl/cu126; TORCH_MOTORE=CUDA
      else _gpu_warn "driver NVIDIA vecchio: le voci useranno la CPU (aggiorna il driver per la GPU)"
      fi
      ;;
  esac
}

_torch_motore() {
  "$1/bin/python" -c 'import torch; print("ROCm" if torch.version.hip else "CUDA" if torch.version.cuda else "CPU")' \
    2>/dev/null || true
}

allinea_torch() {
  local dir=$1 pip="$1/bin/pip" ora
  torch_scelta
  ora=$(_torch_motore "$dir")
  if [[ "$ora" != "$TORCH_MOTORE" ]]; then
    echo "   torch per ${ora:-?}, alla scheda serve quello per $TORCH_MOTORE: lo sostituisco (alcuni GB)…"
    local idx=()
    [[ -n "$TORCH_IDX" ]] && idx=(--index-url "$TORCH_IDX")
    if [[ "$TORCH_MOTORE" != CUDA ]]; then
      _rimuovi_cuda "$pip"
      "$pip" uninstall -y -q triton >/dev/null 2>&1 || true   # quello per CUDA; ROCm usa triton-rocm
    fi
    if ! { _gpu_pip "$pip" install -q --force-reinstall --no-deps torch torchaudio "${idx[@]}" \
           && _gpu_pip "$pip" install -q torch torchaudio "${idx[@]}"; }; then   # poi le dipendenze del nuovo
      _gpu_warn "torch per $TORCH_MOTORE non installato: le voci restano su ${ora:-?}. Rilancia lo script per riprovare"
      return 0
    fi
  fi
  # torchcodec da PyPI è compilato per CUDA: altrove la versione per CPU (usa l'FFmpeg di sistema)
  if [[ "$TORCH_MOTORE" != CUDA ]] && "$pip" show torchcodec >/dev/null 2>&1 \
     && ! "$pip" show torchcodec 2>/dev/null | grep -q '^Version: .*+cpu'; then
    local v
    v=$("$pip" show torchcodec | awk '/^Version:/{print $2}')
    _gpu_pip "$pip" install -q --force-reinstall --no-deps "torchcodec==${v%%+*}+cpu" --index-url "$TORCH_CPU_INDEX" \
      || _gpu_warn "torchcodec per CPU non disponibile: solo un avviso, il Diario non lo usa"
  fi
}
