# shellcheck shell=bash
# Rende visibili ai processi Python che trascrivono le librerie della scheda video:
# ctranslate2 le cerca all'avvio, quindi LD_LIBRARY_PATH va impostato prima di lanciarli.
# Richiede ROOT.
#   NVIDIA  librerie CUDA installate nel venv (scripts/prepara-ai.sh)
#   AMD     ROCm installato da AMD in /opt/rocm (quello dei pacchetti Ubuntu è già nei
#           percorsi di sistema)
CUDA_LIBS=$("$ROOT/.venv/bin/python" -c '
import importlib
out = []
for m in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
    try:
        out.extend(list(importlib.import_module(m).__path__)[:1])
    except Exception:
        pass
print(":".join(out))' 2>/dev/null || true)
[[ -d /opt/rocm/lib ]] && CUDA_LIBS="${CUDA_LIBS:+$CUDA_LIBS:}/opt/rocm/lib"
if [[ -n "$CUDA_LIBS" ]]; then
  export LD_LIBRARY_PATH="$CUDA_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
