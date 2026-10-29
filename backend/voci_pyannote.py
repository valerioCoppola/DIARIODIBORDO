"""Riconoscimento delle voci con pyannote, in un processo a parte.

Gira con l'interprete di .venv-voci (torch + pyannote.audio), separato da quello
del Diario: le librerie non si pestano i piedi e, finito il lavoro, il processo
esce e restituisce tutta la memoria (scheda video compresa).

    # una volta sola, con un token di Hugging Face (non viene salvato):
    HF_TOKEN=hf_... python voci_pyannote.py --scarica DEST

    # dal worker: audio già decodificato, mono float32
    python voci_pyannote.py --audio FILE.f32 --sr 16000 --modello DEST [--persone N]

Stampa su stdout un JSON: {"turni": [[inizio, fine, "SPEAKER_00"], ...], "dispositivo": "cuda"}.
Nessun dato lascia il PC: niente telemetria, Hugging Face in modalità offline.

Scheda video: torch per CUDA (NVIDIA) o per ROCm (AMD); in entrambi i casi il
dispositivo si chiama "cuda". Con ROCm l'LSTM di pyannote passa da MIOpen, che
sulle Radeon recenti (RDNA4) fallisce con miopenStatusUnknownError: lì torch usa
la propria implementazione, sempre sulla scheda video.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO = "pyannote/speaker-diarization-community-1"


def prepara_torch():
    import torch

    if torch.version.hip:
        torch.backends.cudnn.enabled = False   # su ROCm è MIOpen: vedi sopra
    return torch


def motore(torch) -> str:
    return "ROCm" if torch.version.hip else "CUDA" if torch.version.cuda else "CPU"


def prova_gpu(torch) -> str | None:
    """L'LSTM di pyannote (stessa forma) su un lotto piccolo, il caso che con MIOpen fallisce.

    None se la scheda video funziona.
    """
    if not torch.cuda.is_available():
        return None
    try:
        lstm = torch.nn.LSTM(60, 128, num_layers=4, batch_first=True, dropout=0.5, bidirectional=True)
        lstm = lstm.to("cuda").eval()
        with torch.inference_mode():
            lstm(torch.zeros(2, 16, 60, device="cuda"))
        torch.cuda.synchronize()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]


def scarica(dest: str) -> None:
    os.environ["HF_HUB_OFFLINE"] = "0"
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        sys.exit("serve HF_TOKEN")
    from huggingface_hub import snapshot_download

    snapshot_download(REPO, local_dir=dest, token=token)
    if not os.path.isfile(os.path.join(dest, "config.yaml")):
        sys.exit("download incompleto: manca config.yaml")
    print(json.dumps({"scaricato": dest}))


def diarizza(audio: str, sr: int, modello: str, persone: int | None) -> None:
    import numpy as np
    from pyannote.audio import Pipeline

    torch = prepara_torch()

    try:
        from pyannote.audio.telemetry import set_telemetry_metrics

        set_telemetry_metrics(False)
    except Exception:
        pass

    wav = np.fromfile(audio, dtype=np.float32)
    if wav.size == 0:
        print(json.dumps({"turni": [], "dispositivo": "cpu", "secondi": 0}))
        return
    pipe = Pipeline.from_pretrained(modello)
    if pipe is None:
        sys.exit(f"modello non caricabile da {modello}")
    kwargs = {"num_speakers": persone} if persone else {}
    data = {"waveform": torch.from_numpy(wav).unsqueeze(0), "sample_rate": sr}

    def run(device: str):
        pipe.to(torch.device(device))
        return pipe(data, **kwargs)

    t0 = time.monotonic()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        out = run(device)
    except Exception as e:  # memoria video esaurita o driver: si ripiega sulla CPU
        if device != "cuda":
            raise
        print(f"GPU non utilizzabile ({type(e).__name__}): uso la CPU", file=sys.stderr)
        torch.cuda.empty_cache()
        device = "cpu"
        out = run(device)

    # "exclusive": una sola voce per istante, la forma giusta per allinearla al testo.
    ann = getattr(out, "exclusive_speaker_diarization", None) or getattr(out, "speaker_diarization", out)
    turni = [[round(float(t.start), 2), round(float(t.end), 2), str(lab)]
             for t, _, lab in ann.itertracks(yield_label=True)]
    print(json.dumps({"turni": turni, "dispositivo": device, "secondi": round(time.monotonic() - t0, 1)}))


def main() -> None:
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
    p = argparse.ArgumentParser()
    p.add_argument("--scarica", metavar="DEST")
    p.add_argument("--audio")
    p.add_argument("--sr", type=int, default=16000)
    p.add_argument("--modello")
    p.add_argument("--persone", type=int)
    p.add_argument("--verifica", action="store_true", help="solo: le librerie si importano?")
    a = p.parse_args()
    if a.scarica:
        scarica(a.scarica)
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    if a.verifica:
        import pyannote.audio  # noqa: F401

        torch = prepara_torch()
        print(json.dumps({"torch": torch.__version__, "cuda": torch.cuda.is_available(), "motore": motore(torch),
                          "errore_gpu": prova_gpu(torch)}))
        return
    if not (a.audio and a.modello):
        p.error("servono --audio e --modello")
    diarizza(a.audio, a.sr, a.modello, a.persone)


if __name__ == "__main__":
    main()
