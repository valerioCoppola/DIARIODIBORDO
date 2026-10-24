"""Acquisizione degli audio: caricamento a blocchi, conservazione, deduplicazione, job.

Flusso (spec. §2B e §9):
  1. init      -> sessione di caricamento identificata da una chiave di idempotenza
  2. chunk ×N  -> ogni blocco è scritto su disco in modo atomico; ripetibile
  3. complete  -> ricomposizione, verifica sha256, conservazione dell'originale,
                  creazione di recording + ingestion_job in stato 'originale_conservato'
Le fasi successive (trascrizione, analisi, indicizzazione) sono del worker
(Incremento 2): il caricamento riuscito NON equivale a nota archiviata.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from fastapi import HTTPException, status

from . import storage
from .config import get_settings
from .db import user_tx

VALID_SOURCES = {"registratore", "caricamento", "api", "altro"}
VALID_TIME_SOURCES = {"registratore_integrato", "metadati_file", "dichiarato_utente"}


class IngestError(HTTPException):
    def __init__(self, code: int, error: str, message: str, **extra: Any) -> None:
        super().__init__(code, {"errore": error, "messaggio": message, **extra})


@dataclass
class RecordingMeta:
    source: str
    mime_type: str
    filename: str | None = None
    recorded_at: datetime | None = None
    recorded_at_source: str | None = None
    duration_seconds: float | None = None
    client_metadata: dict | None = None
    # Postgres restituisce le date in UTC: l'offset originale viaggia a parte.
    utc_offset_minutes: int | None = None


def validate_meta(meta: RecordingMeta) -> RecordingMeta:
    s = get_settings()
    if meta.source not in VALID_SOURCES:
        raise IngestError(422, "provenienza_non_valida", f"Provenienza non ammessa: {meta.source}")
    mime = storage.base_mime(meta.mime_type)
    if not any(mime.startswith(p) for p in s.allowed_audio_mime_prefixes):
        raise IngestError(415, "formato_non_supportato", f"Tipo di file non audio: {mime}")
    if meta.recorded_at is not None:
        if meta.recorded_at.utcoffset() is None:
            raise IngestError(422, "data_senza_offset", "recorded_at deve includere l'offset orario")
        if meta.recorded_at_source not in VALID_TIME_SOURCES:
            raise IngestError(
                422, "fonte_data_mancante",
                "recorded_at richiede recorded_at_source (registratore_integrato, metadati_file, dichiarato_utente)",
            )
        if meta.recorded_at > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise IngestError(422, "data_nel_futuro", "recorded_at è nel futuro")
        if meta.utc_offset_minutes is None:
            meta.utc_offset_minutes = _offset_minutes(meta.recorded_at)
    else:
        meta.recorded_at_source = None
        meta.utc_offset_minutes = None
    if meta.duration_seconds is not None and meta.duration_seconds < 0:
        raise IngestError(422, "durata_non_valida", "Durata negativa")
    if meta.filename:
        meta.filename = os.path.basename(meta.filename)[:255]
    return meta


def _offset_minutes(dt: datetime | None) -> int | None:
    if dt is None or dt.utcoffset() is None:
        return None
    return int(dt.utcoffset().total_seconds() // 60)


def _job_event(conn: psycopg.Connection, owner: UUID, job_id: UUID, frm: str | None, to: str, msg: str | None = None) -> None:
    conn.execute(
        "INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) VALUES (%s, %s, %s, %s, %s)",
        (owner, job_id, frm, to, msg),
    )


def _existing_by_sha(conn: psycopg.Connection, sha: bytes) -> dict | None:
    rec = conn.execute("SELECT id FROM recordings WHERE sha256 = %s", (sha,)).fetchone()
    if rec is None:
        return None
    job = conn.execute(
        "SELECT id, status FROM ingestion_jobs WHERE recording_id = %s ORDER BY created_at LIMIT 1",
        (rec["id"],),
    ).fetchone()
    return {"recording_id": rec["id"], "job_id": job["id"] if job else None,
            "job_status": job["status"] if job else None}


def finalize_original(conn: psycopg.Connection, owner: UUID, tmp_file: Path, sha_hex: str,
                      size: int, meta: RecordingMeta) -> dict:
    """Conserva l'originale e crea recording + job. Va chiamata dentro user_tx.

    Il file viene spostato prima del commit: se il commit fallisce resta un file
    nella sua posizione derivata dall'impronta, e il tentativo successivo lo
    riconosce senza duplicarlo.
    """
    sha = bytes.fromhex(sha_hex)
    existing = _existing_by_sha(conn, sha)
    if existing:
        tmp_file.unlink(missing_ok=True)
        return {**existing, "duplicato": True, "sha256": sha_hex}

    diario = conn.execute("SELECT app_current_diario() AS d").fetchone()["d"]
    rel = storage.relative_path_for(owner, diario, sha_hex, meta.mime_type)
    storage.store_original(tmp_file, rel)

    rec = conn.execute(
        """INSERT INTO recordings (owner_id, source, storage_path, original_filename, mime_type, format,
                                   size_bytes, duration_seconds, sha256, recorded_at, recorded_at_source,
                                   recorded_utc_offset_minutes, client_metadata, storage_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'conservato')
           RETURNING id""",
        (owner, meta.source, rel, meta.filename, meta.mime_type, storage.describe_format(meta.mime_type),
         size, meta.duration_seconds, sha, meta.recorded_at, meta.recorded_at_source,
         meta.utc_offset_minutes, Jsonb(meta.client_metadata or {})),
    ).fetchone()

    job = conn.execute(
        """INSERT INTO ingestion_jobs (owner_id, kind, idempotency_key, recording_id, status)
           VALUES (%s, 'audio', %s, %s, 'originale_conservato') RETURNING id""",
        (owner, f"audio:{sha_hex}", rec["id"]),
    ).fetchone()
    _job_event(conn, owner, job["id"], None, "ricevuto")
    _job_event(conn, owner, job["id"], "ricevuto", "originale_conservato", f"{size} byte, sha256 verificato")
    return {"recording_id": rec["id"], "job_id": job["id"], "job_status": "originale_conservato",
            "duplicato": False, "sha256": sha_hex}


# ---------------------------------------------------------------------------
# Sessioni di caricamento a blocchi
# ---------------------------------------------------------------------------


def _session_view(row: dict) -> dict:
    received = sorted(row["received_chunks"] or [])
    missing = sorted(set(range(row["total_chunks"])) - set(received))
    return {
        "id": row["id"],
        "stato": row["status"],
        "dimensione_totale": row["total_size"],
        "dimensione_blocco": row["chunk_size"],
        "numero_blocchi": row["total_chunks"],
        "blocchi_ricevuti": received,
        "blocchi_mancanti": missing,
        "recording_id": row["recording_id"],
        "job_id": row["job_id"],
        "duplicato": row["was_duplicate"],
        "scade_il": row["expires_at"],
    }


def init_upload(owner: UUID, diario: UUID, idempotency_key: str, meta: RecordingMeta, total_size: int,
                chunk_size: int | None, expected_sha256: str | None) -> tuple[dict, bool]:
    """Crea o riprende una sessione. Restituisce (vista, creata_ora)."""
    s = get_settings()
    meta = validate_meta(meta)
    if not 8 <= len(idempotency_key) <= 200:
        raise IngestError(422, "chiave_idempotenza_non_valida", "Idempotency-Key deve avere 8–200 caratteri")
    if total_size <= 0 or total_size > s.max_upload_bytes:
        raise IngestError(413, "dimensione_non_ammessa", f"Dimensione massima {s.max_upload_bytes} byte")
    chunk_size = chunk_size or s.default_chunk_bytes
    if not 64 * 1024 <= chunk_size <= s.max_chunk_bytes:
        raise IngestError(422, "blocco_non_valido", f"Blocco tra 65536 e {s.max_chunk_bytes} byte")
    sha = None
    if expected_sha256:
        try:
            sha = bytes.fromhex(expected_sha256)
            assert len(sha) == 32
        except (ValueError, AssertionError):
            raise IngestError(422, "impronta_non_valida", "sha256 deve essere esadecimale di 64 caratteri")
    total_chunks = math.ceil(total_size / chunk_size)
    expires = datetime.now(timezone.utc) + timedelta(hours=s.upload_session_ttl_hours)

    with user_tx(owner, diario) as conn:
        row = conn.execute(
            """INSERT INTO upload_sessions (owner_id, idempotency_key, source, filename, mime_type, total_size,
                                            chunk_size, total_chunks, expected_sha256, recorded_at,
                                            recorded_at_source, recorded_utc_offset_minutes, duration_seconds,
                                            client_metadata, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (owner_id, diario_id, idempotency_key) DO NOTHING
               RETURNING *""",
            (owner, idempotency_key, meta.source, meta.filename, meta.mime_type, total_size, chunk_size,
             total_chunks, sha, meta.recorded_at, meta.recorded_at_source, meta.utc_offset_minutes,
             meta.duration_seconds, Jsonb(meta.client_metadata or {}), expires),
        ).fetchone()
        created = row is not None
        if not created:
            row = conn.execute(
                "SELECT * FROM upload_sessions WHERE idempotency_key = %s FOR UPDATE", (idempotency_key,)
            ).fetchone()
            if row["total_size"] != total_size or row["chunk_size"] != chunk_size or (
                sha is not None and row["expected_sha256"] is not None and bytes(row["expected_sha256"]) != sha
            ):
                raise IngestError(409, "chiave_riutilizzata",
                                  "Idempotency-Key già usata per un caricamento con parametri diversi")
            if row["status"] == "aperta" and row["expires_at"] < datetime.now(timezone.utc):
                raise IngestError(410, "sessione_scaduta", "Sessione di caricamento scaduta: avviarne una nuova")
            return _session_view(row), False

        # Contenuto già presente: nessun byte da inviare.
        if sha is not None:
            existing = _existing_by_sha(conn, sha)
            if existing:
                row = conn.execute(
                    """UPDATE upload_sessions SET status = 'completata', recording_id = %s, job_id = %s,
                              was_duplicate = true WHERE id = %s RETURNING *""",
                    (existing["recording_id"], existing["job_id"], row["id"]),
                ).fetchone()
        return _session_view(row), created


def get_upload(owner: UUID, diario: UUID, upload_id: UUID) -> dict:
    with user_tx(owner, diario) as conn:
        row = conn.execute("SELECT * FROM upload_sessions WHERE id = %s", (upload_id,)).fetchone()
    if row is None:
        raise IngestError(404, "sessione_inesistente", "Sessione di caricamento non trovata")
    return _session_view(row)


def put_chunk(owner: UUID, diario: UUID, upload_id: UUID, index: int, data: bytes, chunk_sha256: str | None) -> dict:
    with user_tx(owner, diario) as conn:
        row = conn.execute("SELECT * FROM upload_sessions WHERE id = %s", (upload_id,)).fetchone()
    if row is None:
        raise IngestError(404, "sessione_inesistente", "Sessione di caricamento non trovata")
    if row["status"] == "completata":
        return _session_view(row)  # ripetizione innocua dopo il completamento
    if row["status"] != "aperta":
        raise IngestError(409, "sessione_chiusa", f"Sessione in stato {row['status']}")
    if row["expires_at"] < datetime.now(timezone.utc):
        raise IngestError(410, "sessione_scaduta", "Sessione di caricamento scaduta")
    if not 0 <= index < row["total_chunks"]:
        raise IngestError(422, "indice_non_valido", f"Indice blocco fuori intervallo (0–{row['total_chunks'] - 1})")
    is_last = index == row["total_chunks"] - 1
    expected_len = row["total_size"] - row["chunk_size"] * (row["total_chunks"] - 1) if is_last else row["chunk_size"]
    if len(data) != expected_len:
        raise IngestError(422, "lunghezza_blocco_errata", f"Il blocco {index} deve essere di {expected_len} byte",
                          ricevuti=len(data))
    if chunk_sha256 and hashlib.sha256(data).hexdigest() != chunk_sha256.lower():
        raise IngestError(422, "impronta_blocco_errata", f"sha256 del blocco {index} non corrisponde")

    storage.write_chunk_atomic(upload_id, index, data)

    with user_tx(owner, diario) as conn:
        row = conn.execute(
            """UPDATE upload_sessions
                  SET received_chunks = ARRAY(SELECT DISTINCT x FROM unnest(received_chunks || %s::int) x ORDER BY x)
                WHERE id = %s RETURNING *""",
            (index, upload_id),
        ).fetchone()
    return _session_view(row)


def complete_upload(owner: UUID, diario: UUID, upload_id: UUID) -> dict:
    with user_tx(owner, diario) as conn:
        row = conn.execute("SELECT * FROM upload_sessions WHERE id = %s FOR UPDATE", (upload_id,)).fetchone()
        if row is None:
            raise IngestError(404, "sessione_inesistente", "Sessione di caricamento non trovata")
        view = _session_view(row)
        if row["status"] == "completata":
            return {**view, "job_status": _job_status(conn, row["job_id"])}
        if row["status"] != "aperta":
            raise IngestError(409, "sessione_chiusa", f"Sessione in stato {row['status']}")
        if view["blocchi_mancanti"]:
            raise IngestError(409, "blocchi_mancanti", "Mancano dei blocchi", blocchi_mancanti=view["blocchi_mancanti"])

        tmp, sha_hex, size = storage.assemble_upload(upload_id, row["total_chunks"])
        if size != row["total_size"]:
            tmp.unlink(missing_ok=True)
            raise IngestError(422, "dimensione_errata", "La dimensione ricomposta non corrisponde")
        mismatch = row["expected_sha256"] is not None and bytes(row["expected_sha256"]).hex() != sha_hex
        if mismatch:
            # Il reset viene salvato: la transazione termina normalmente e l'errore
            # è sollevato subito dopo, fuori dal blocco.
            tmp.unlink(missing_ok=True)
            conn.execute("UPDATE upload_sessions SET received_chunks = '{}' WHERE id = %s", (upload_id,))
        else:
            meta = RecordingMeta(
                source=row["source"], mime_type=row["mime_type"], filename=row["filename"],
                recorded_at=row["recorded_at"], recorded_at_source=row["recorded_at_source"],
                duration_seconds=float(row["duration_seconds"]) if row["duration_seconds"] is not None else None,
                client_metadata=row["client_metadata"],
                utc_offset_minutes=row["recorded_utc_offset_minutes"],
            )
            result = finalize_original(conn, owner, tmp, sha_hex, size, meta)
            row = conn.execute(
                """UPDATE upload_sessions SET status = 'completata', recording_id = %s, job_id = %s,
                          was_duplicate = %s WHERE id = %s RETURNING *""",
                (result["recording_id"], result["job_id"], result["duplicato"], upload_id),
            ).fetchone()
    storage.discard_upload(upload_id)
    if mismatch:
        raise IngestError(422, "impronta_non_corrispondente",
                          "L'impronta del file ricomposto non corrisponde: i blocchi vanno reinviati")
    return {**_session_view(row), "job_status": result["job_status"]}


def cancel_upload(owner: UUID, diario: UUID, upload_id: UUID) -> dict:
    with user_tx(owner, diario) as conn:
        row = conn.execute(
            "UPDATE upload_sessions SET status = 'annullata' WHERE id = %s AND status = 'aperta' RETURNING *",
            (upload_id,),
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM upload_sessions WHERE id = %s", (upload_id,)).fetchone()
    if row is None:
        raise IngestError(404, "sessione_inesistente", "Sessione di caricamento non trovata")
    storage.discard_upload(upload_id)
    return _session_view(row)


def _job_status(conn: psycopg.Connection, job_id: UUID | None) -> str | None:
    if job_id is None:
        return None
    r = conn.execute("SELECT status FROM ingestion_jobs WHERE id = %s", (job_id,)).fetchone()
    return r["status"] if r else None


# ---------------------------------------------------------------------------
# Caricamento in un'unica richiesta (file piccoli, script, curl)
# ---------------------------------------------------------------------------


def new_spool_file() -> tuple[Path, Any]:
    d = get_settings().upload_tmp_dir
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="single-", suffix=".tmp", dir=d)
    os.chmod(name, 0o600)
    return Path(name), os.fdopen(fd, "wb")


def ingest_single(owner: UUID, diario: UUID, tmp: Path, sha_hex: str, size: int, meta: RecordingMeta) -> dict:
    meta = validate_meta(meta)
    with user_tx(owner, diario) as conn:
        return finalize_original(conn, owner, tmp, sha_hex, size, meta)


def cleanup_expired_uploads(owner: UUID, diario: UUID) -> int:
    with user_tx(owner, diario) as conn:
        rows = conn.execute(
            "UPDATE upload_sessions SET status = 'scaduta' WHERE status = 'aperta' AND expires_at < now() RETURNING id"
        ).fetchall()
    for r in rows:
        storage.discard_upload(r["id"])
    return len(rows)
