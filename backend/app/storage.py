"""Storage privato su disco per gli audio originali.

- I file vivono fuori dalla cartella del frontend e sono leggibili solo
  dall'utente di sistema che esegue il servizio (permessi 0600/0700).
- Il percorso è derivato dall'impronta sha256: nessun nome fornito
  dall'utente finisce nel filesystem, quindi niente path traversal.
- La scrittura è atomica (file temporaneo + fsync + rename): un'interruzione
  non lascia mai un originale troncato al suo posto definitivo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from uuid import UUID

from .config import get_settings

DIR_MODE = 0o700
FILE_MODE = 0o600

_EXT_BY_MIME = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


def base_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def extension_for(mime: str) -> str:
    return _EXT_BY_MIME.get(base_mime(mime), "bin")


def describe_format(mime: str) -> str:
    """Es. 'audio/webm;codecs=opus' -> 'webm/opus'."""
    base = base_mime(mime)
    codec = ""
    for part in mime.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.strip().lower() == "codecs":
            codec = v.strip().strip('"')
    sub = base.split("/", 1)[-1]
    return f"{sub}/{codec}" if codec else sub


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(path, DIR_MODE)


def ensure_storage() -> None:
    s = get_settings()
    _ensure_dir(s.storage_dir)
    _ensure_dir(s.upload_tmp_dir)


def relative_path_for(owner_id: UUID, diario_id: UUID, sha256_hex: str, mime: str) -> str:
    """Una cartella per diario: lo stesso audio in due diari sono due file, ciascuno eliminabile.

    Gli originali arrivati prima dei diari restano dov'erano (<proprietario>/<xx>/…).
    """
    return f"{owner_id}/{diario_id}/{sha256_hex[:2]}/{sha256_hex}.{extension_for(mime)}"


def absolute_path(relative: str) -> Path:
    root = get_settings().storage_dir.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("percorso fuori dallo storage")
    return path


# ---------------------------------------------------------------------------
# Blocchi di caricamento
# ---------------------------------------------------------------------------


def upload_dir(upload_id: UUID) -> Path:
    return get_settings().upload_tmp_dir / str(upload_id)


def chunk_path(upload_id: UUID, index: int) -> Path:
    return upload_dir(upload_id) / f"{index:06d}.part"


def write_chunk_atomic(upload_id: UUID, index: int, data: bytes) -> None:
    d = upload_dir(upload_id)
    _ensure_dir(d)
    final = chunk_path(upload_id, index)
    tmp = final.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, final)


def assemble_upload(upload_id: UUID, total_chunks: int) -> tuple[Path, str, int]:
    """Concatena i blocchi in un file temporaneo; restituisce (percorso, sha256, dimensione)."""
    d = upload_dir(upload_id)
    out = d / "assembled.tmp"
    h = hashlib.sha256()
    size = 0
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "wb") as dst:
        for i in range(total_chunks):
            with open(chunk_path(upload_id, i), "rb") as src:
                while block := src.read(1024 * 1024):
                    h.update(block)
                    size += len(block)
                    dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    return out, h.hexdigest(), size


def store_original(tmp_file: Path, relative: str) -> bool:
    """Sposta il file nella posizione definitiva. False se esisteva già (stesso contenuto)."""
    dest = absolute_path(relative)
    _ensure_dir(dest.parent)
    if dest.exists():
        tmp_file.unlink(missing_ok=True)
        return False
    os.chmod(tmp_file, FILE_MODE)
    os.replace(tmp_file, dest)
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True


def discard_upload(upload_id: UUID) -> None:
    shutil.rmtree(upload_dir(upload_id), ignore_errors=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()
