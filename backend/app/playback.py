"""Copia d'ascolto degli originali.

I browser registrano a pezzi: il WebM di Chrome/Android non dichiara la durata
e l'MP4 frammentato di Safari/iPhone si comporta male nei lettori (durata
assente, cursore che non si sposta, riproduzione che si ferma). Per ascoltare
si usa quindi una copia MP3 a bitrate costante (mono, 64 kbit/s): ogni browser
la riproduce, anche i Chromium senza codec AAC, e con il bitrate costante la
posizione nel file è esatta, quindi il cursore e "tocca una frase" vanno dritti
al punto. L'originale resta intatto e scaricabile.

La copia sta accanto all'originale (<sha256>.ascolto.mp3), con gli stessi
permessi; la prepara il worker all'arrivo del vocale, oppure la prima
richiesta di ascolto per i vocali più vecchi.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("diario.ascolto")

SUFFIX = ".ascolto.mp3"
MEDIA_TYPE = "audio/mpeg"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def playback_path(original: Path) -> Path:
    return original.with_name(original.name.split(".", 1)[0] + SUFFIX)


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def ensure_playback(original: Path) -> Path | None:
    """Percorso della copia d'ascolto, creata se manca. None se non si riesce a crearla."""
    dst = playback_path(original)
    if dst.is_file():
        return dst
    with _lock_for(dst):
        if dst.is_file():
            return dst
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            _transcode(original, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dst)
            return dst
        except Exception as e:  # l'ascolto ripiega sull'originale
            log.warning("copia d'ascolto non creata per %s: %s: %s", original.name, type(e).__name__, e)
            tmp.unlink(missing_ok=True)
            return None


def _transcode(src: Path, dst: Path) -> None:
    import av

    with av.open(str(src)) as inp:
        if not inp.streams.audio:
            raise ValueError("nessuna traccia audio")
        ias = inp.streams.audio[0]
        with av.open(str(dst), "w", format="mp3") as out:
            oas = out.add_stream("libmp3lame", rate=44100, layout="mono")
            oas.bit_rate = 64000
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)
            for frame in inp.decode(ias):
                for rf in resampler.resample(frame):
                    for packet in oas.encode(rf):
                        out.mux(packet)
            for rf in resampler.resample(None):
                for packet in oas.encode(rf):
                    out.mux(packet)
            for packet in oas.encode(None):
                out.mux(packet)


def remove_files(original: Path) -> None:
    """Cancella originale e copia d'ascolto (se ci sono)."""
    for p in (original, playback_path(original)):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("impossibile cancellare %s: %s", p.name, e)
