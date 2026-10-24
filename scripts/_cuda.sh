# shellcheck shell=bash
# Rende visibili le librerie CUDA installate nel venv (scripts/prepara-ai.sh) ai
# processi Python che trascrivono: ctranslate2 le cerca all'avvio, quindi
# LD_LIBRARY_PATH va impostato prima di lanciarli. Richiede ROOT.
CUDA_LIBS=$("$ROOT/.venv/bin/python" -c '
import importlib
out = []
for m in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
    try:
        out.extend(list(importlib.import_module(m).__path__)[:1])
    except Exception:
        pass
print(":".join(out))' 2>/dev/null || true)
if [[ -n "$CUDA_LIBS" ]]; then
  export LD_LIBRARY_PATH="$CUDA_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
