"""Voci: chi parla, segmento per segmento.

Il riconoscimento (pyannote) gira in un processo a parte con l'interprete di
.venv-voci (vedi voci_pyannote.py); qui si decodifica l'audio, si lancia il
processo e si allineano i turni di parola ai segmenti della trascrizione, che
hanno già i loro tempi. Funziona anche su trascrizioni fatte prima: non serve
ritrascrivere.

Le voci sono numerate dalla prima che parla: "Voce 1", "Voce 2"… A ogni nota si
possono dare i nomi veri ({"1": "Marco"}), usati nella pagina e nelle analisi.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .pipeline.errors import PipelineError

log = logging.getLogger("diario.voci")

SCRIPT = Path(__file__).resolve().parents[1] / "voci_pyannote.py"
SAMPLE_RATE = 16000
# Un segmento senza turni sovrapposti prende la voce del turno più vicino entro questa distanza.
MAX_GAP_SECONDS = 1.5


@dataclass
class EsitoVoci:
    turni: list[tuple[float, float, str]]
    dispositivo: str
    secondi: float


class Diarizzatore:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def disponibile(self) -> bool:
        return Path(self.s.voci_python).is_file() and (Path(self.s.voci_modello) / "config.yaml").is_file()

    def describe(self) -> dict:
        return {"pronto": self.disponibile(), "modello": "pyannote community-1"}

    def diarizza(self, audio_path: Path, persone: int | None = None) -> EsitoVoci:
        from faster_whisper.audio import decode_audio

        wav = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
        tmp_dir = Path(self.s.upload_tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".f32", dir=tmp_dir)
        os.close(fd)
        try:
            wav.astype("float32").tofile(tmp)
            del wav
            cmd = [str(self.s.voci_python), str(SCRIPT), "--audio", tmp, "--sr", str(SAMPLE_RATE),
                   "--modello", str(self.s.voci_modello)]
            if persone:
                cmd += ["--persone", str(persone)]
            env = {**os.environ, "PYANNOTE_METRICS_ENABLED": "false", "HF_HUB_OFFLINE": "1"}
            t0 = time.monotonic()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=self.s.voci_timeout_seconds)
            except subprocess.TimeoutExpired as e:
                raise PipelineError("riconoscimento delle voci troppo lungo", code="voci_timeout") from e
            if p.returncode != 0:
                tail = (p.stderr or "").strip().splitlines()[-3:]
                raise PipelineError("riconoscimento delle voci non riuscito: " + " | ".join(tail)[:400],
                                    code="voci_errore")
            out = json.loads(p.stdout.strip().splitlines()[-1])
            turni = [(float(a), float(b), str(lab)) for a, b, lab in out.get("turni", [])]
            return EsitoVoci(turni, out.get("dispositivo", "?"), float(out.get("secondi") or time.monotonic() - t0))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def assegna_voci(segments: list[dict], turni: list[tuple[float, float, str]]) -> tuple[list[dict], int]:
    """A ogni segmento la voce che vi parla più a lungo; le voci numerate dalla prima che parla."""
    numeri: dict[str, int] = {}
    out: list[dict] = []
    for s in segments:
        a, b = float(s.get("inizio", 0)), float(s.get("fine", 0))
        durate: dict[str, float] = {}
        for ta, tb, lab in turni:
            ov = min(b, tb) - max(a, ta)
            if ov > 0:
                durate[lab] = durate.get(lab, 0.0) + ov
        lab = max(durate, key=durate.get) if durate else None
        if lab is None and turni:
            mid = (a + b) / 2
            dist, lab = min((min(abs(mid - ta), abs(mid - tb)), l) for ta, tb, l in turni)
            if dist > MAX_GAP_SECONDS:
                lab = None
        seg = {k: v for k, v in s.items() if k != "voce"}
        if lab is not None:
            if lab not in numeri:
                numeri[lab] = len(numeri) + 1
            seg["voce"] = numeri[lab]
        out.append(seg)
    return out, len(numeri)


def numero_voci(segments: list[dict]) -> int:
    return len({s["voce"] for s in segments if s.get("voce")})


def etichetta(voce: int, nomi: dict | None) -> str:
    nome = (nomi or {}).get(str(voce))
    return nome.strip() if isinstance(nome, str) and nome.strip() else f"Voce {voce}"


def etichette(segments: list[dict], nomi: dict | None) -> set[str]:
    return {etichetta(v, nomi) for v in {s["voce"] for s in segments if s.get("voce")}}


def turni(segments: list[dict]) -> list[tuple[int | None, int, int]]:
    """Interventi: segmenti consecutivi della stessa voce, come (voce, char_start, char_end)."""
    out: list[tuple[int | None, int, int]] = []
    for s in segments:
        v = s.get("voce")
        if out and out[-1][0] == v:
            out[-1] = (v, out[-1][1], s["char_end"])
        else:
            out.append((v, s["char_start"], s["char_end"]))
    return out


def testo_con_voci(testo: str, segments: list[dict], nomi: dict | None) -> str | None:
    """La trascrizione come dialogo ("Marco: …"), per l'analisi. None se parla una sola voce."""
    if numero_voci(segments) < 2:
        return None
    righe = []
    for v, a, b in turni(segments):
        frase = " ".join(testo[a:b].split())
        if frase:
            righe.append(f"{etichetta(v, nomi) if v else 'Voce ?'}: {frase}")
    return "\n\n".join(righe)
