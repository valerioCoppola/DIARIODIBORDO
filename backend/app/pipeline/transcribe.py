"""Trascrizione locale con faster-whisper, sulla GPU se disponibile.

Scelta automatica di modello e precisione in base alla memoria video:

    GPU ≥ 10 GB   large-v3        float16
    GPU ≥  6 GB   large-v3        int8_float16
    GPU <  6 GB   large-v3-turbo  int8_float16
    senza GPU     large-v3-turbo  int8 su CPU (più lento, stessa qualità di turbo)

Se la GPU fallisce (librerie CUDA mancanti, memoria esaurita) la trascrizione
riparte da sola sulla CPU e lo segnala nei log: un vocale non resta bloccato
per un problema di driver.

Il testo è quello prodotto dal modello, segmento per segmento, senza
riscritture. Per leggibilità va a capo: dopo una pausa lunga, oppure a fine frase
dopo una pausa breve o quando il paragrafo è già lungo. Ogni segmento conserva
inizio e fine nell'audio e la sua posizione nel testo.

Glossario: i nomi che ricorrono nell'archivio (voci con un nome, temi)
vengono suggeriti a Whisper come hotwords, in ogni finestra di 30 secondi.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from .errors import PermanentError, ServiceUnavailable

log = logging.getLogger("diario.trascrizione")

PAUSE_PARAGRAPH_SECONDS = 2.5      # a capo comunque
PAUSE_SENTENCE_SECONDS = 1.2       # a capo se la frase prima è finita
PARAGRAPH_MAX_CHARS = 600          # oltre, a capo alla prima fine frase
_FINE_FRASE = (".", "!", "?", "…")
_CUDA_HINTS = ("cuda", "cublas", "cudnn", "out of memory", "libcu", "gpu")


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    text: str
    segments: list[dict]
    language: str | None
    language_probability: float | None
    audio_seconds: float | None
    speech_seconds: float | None
    model: str
    device: str
    compute_type: str
    elapsed_ms: int


@dataclass(frozen=True)
class WhisperChoice:
    model: str
    device: str
    compute_type: str
    keep_loaded: bool


def assemble(segments: list[Segment]) -> tuple[str, list[dict]]:
    """Unisce i segmenti in un testo e registra dove cade ciascuno."""
    parts: list[str] = []
    out: list[dict] = []
    pos = 0
    prev_end: float | None = None
    prev_text = ""
    par_start = 0
    for s in segments:
        t = s.text.strip()
        if not t:
            continue
        if parts:
            pausa = s.start - prev_end if prev_end is not None else 0.0
            finita = prev_text.endswith(_FINE_FRASE)
            nuovo = pausa >= PAUSE_PARAGRAPH_SECONDS or (finita and (
                pausa >= PAUSE_SENTENCE_SECONDS or pos - par_start >= PARAGRAPH_MAX_CHARS))
            sep = "\n\n" if nuovo else " "
            parts.append(sep)
            pos += len(sep)
            if nuovo:
                par_start = pos
        prev_text = t
        parts.append(t)
        out.append({"inizio": round(s.start, 2), "fine": round(s.end, 2),
                    "char_start": pos, "char_end": pos + len(t)})
        pos += len(t)
        prev_end = s.end
    return "".join(parts), out


def gpu_memory_mb() -> int | None:
    """Memoria della prima GPU NVIDIA, se nvidia-smi c'è e risponde."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        values = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        return max(values) if values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def choose(settings: Settings, cuda_devices: int, vram_mb: int | None) -> WhisperChoice:
    device = settings.whisper_device.lower()
    if device == "auto":
        device = "cuda" if cuda_devices > 0 else "cpu"
    v = vram_mb or 0
    model = settings.whisper_model
    compute = settings.whisper_compute_type.lower()
    if device == "cuda":
        if model == "auto":
            model = "large-v3" if v >= 6000 else "large-v3-turbo"
        if compute == "auto":
            compute = "float16" if v >= 10000 else "int8_float16"
    else:
        if model == "auto":
            model = "large-v3-turbo"
        if compute == "auto":
            compute = "int8"
    keep = settings.whisper_keep_loaded.lower()
    keep_loaded = (device == "cuda" and v >= 16000) if keep == "auto" else keep in ("1", "true", "si", "sì", "yes")
    return WhisperChoice(model, device, compute, keep_loaded)


def _looks_like_cuda_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return any(h in msg for h in _CUDA_HINTS)


class FasterWhisperTranscriber:
    """Carica il modello alla prima richiesta; una trascrizione alla volta."""

    def __init__(self, settings: Settings, model_factory: Callable[..., Any] | None = None,
                 cuda_devices: int | None = None, vram_mb: int | None = None) -> None:
        self.settings = settings
        self._factory = model_factory
        self._cuda = cuda_device_count() if cuda_devices is None else cuda_devices
        self._vram = gpu_memory_mb() if vram_mb is None else vram_mb
        self.choice = choose(settings, self._cuda, self._vram)
        self._model: Any = None
        self._lock = threading.Lock()

    def describe(self) -> dict:
        c = self.choice
        return {"modello": f"faster-whisper {c.model}", "dispositivo": c.device, "precisione": c.compute_type,
                "memoria_video_mb": self._vram, "resta_in_memoria": c.keep_loaded}

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        c = self.choice
        factory = self._factory
        if factory is None:
            from faster_whisper import WhisperModel

            factory = WhisperModel
        t0 = time.monotonic()
        try:
            self._model = factory(c.model, device=c.device, compute_type=c.compute_type)
        except Exception as e:
            if c.device == "cuda" and _looks_like_cuda_error(e):
                raise
            # Tipicamente il primo download del modello senza rete.
            raise ServiceUnavailable(f"modello di trascrizione non caricabile ({c.model}): {type(e).__name__}: {e}",
                                     code="modello_trascrizione_non_disponibile") from e
        log.info("modello %s caricato su %s (%s) in %.1fs", c.model, c.device, c.compute_type, time.monotonic() - t0)
        return self._model

    def unload(self) -> None:
        self._model = None  # ctranslate2 libera la memoria video alla distruzione

    def _fallback_to_cpu(self, e: BaseException) -> None:
        log.warning("GPU non utilizzabile per la trascrizione (%s): passo alla CPU", e)
        self.unload()
        c = self.choice
        model = c.model if self.settings.whisper_model != "auto" else "large-v3-turbo"
        self.choice = WhisperChoice(model, "cpu", "int8", c.keep_loaded)

    def _run(self, path: Path, glossario: str | None = None) -> tuple[list[Segment], Any]:
        model = self._load()
        lang = self.settings.whisper_language
        extra: dict[str, Any] = {}
        if glossario:
            extra["hotwords"] = glossario
        segments, info = model.transcribe(
            str(path),
            language=None if lang in ("", "auto") else lang,
            beam_size=self.settings.whisper_beam_size,
            vad_filter=True,                    # salta i silenzi: meno allucinazioni
            condition_on_previous_text=False,   # evita ripetizioni a catena
            **extra,
        )
        # Il generatore fa il lavoro vero mentre lo si consuma.
        return [Segment(float(s.start), float(s.end), s.text) for s in segments], info

    def transcribe(self, path: Path, glossario: str | None = None) -> TranscriptResult:
        with self._lock:
            t0 = time.monotonic()
            try:
                try:
                    segs, info = self._run(path, glossario)
                except PermanentError:
                    raise
                except ServiceUnavailable:
                    raise
                except Exception as e:
                    if self.choice.device == "cuda" and _looks_like_cuda_error(e):
                        self._fallback_to_cpu(e)
                        segs, info = self._run(path, glossario)
                    else:
                        raise
            except (PermanentError, ServiceUnavailable):
                raise
            except Exception as e:
                if _is_decode_error(e):
                    raise PermanentError(f"audio non decodificabile: {type(e).__name__}",
                                         code="audio_illeggibile") from e
                raise
            finally:
                if not self.choice.keep_loaded:
                    self.unload()
            text, seg_out = assemble(segs)
            c = self.choice
            return TranscriptResult(
                text=text, segments=seg_out,
                language=getattr(info, "language", None),
                language_probability=_float(getattr(info, "language_probability", None)),
                audio_seconds=_float(getattr(info, "duration", None)),
                speech_seconds=_float(getattr(info, "duration_after_vad", None)),
                model=f"faster-whisper {c.model}", device=c.device, compute_type=c.compute_type,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _is_decode_error(e: BaseException) -> bool:
    try:
        import av

        if isinstance(e, av.error.FFmpegError):
            return True
    except Exception:
        pass
    return type(e).__name__ in ("InvalidDataError", "EOFError")
