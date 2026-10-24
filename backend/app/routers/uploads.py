"""Endpoint di acquisizione audio."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import ingest
from ..config import get_settings
from ..ingest import IngestError, RecordingMeta
from ..security import CurrentUser, CurrentUserDep

router = APIRouter(prefix="/api/v1", tags=["acquisizione"])


class UploadInit(BaseModel):
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str = Field(max_length=120)
    total_size: int = Field(gt=0)
    chunk_size: int | None = None
    sha256: str | None = Field(default=None, description="Impronta dell'intero file, se nota al client")
    source: str = "registratore"
    recorded_at: datetime | None = Field(default=None, description="Inizio effettivo, ISO 8601 con offset")
    recorded_at_source: str | None = None
    duration_seconds: float | None = None
    client_metadata: dict = Field(default_factory=dict)


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
def init_upload(body: UploadInit, response: Response,
                idempotency_key: str = Header(..., alias="Idempotency-Key"),
                user: CurrentUser = CurrentUserDep) -> dict:
    """Crea una sessione di caricamento, o restituisce quella esistente per la stessa chiave.

    Riprendere dopo un'interruzione = ripetere questa chiamata con la stessa
    Idempotency-Key e inviare solo `blocchi_mancanti`.
    """
    meta = RecordingMeta(
        source=body.source, mime_type=body.mime_type, filename=body.filename,
        recorded_at=body.recorded_at, recorded_at_source=body.recorded_at_source,
        duration_seconds=body.duration_seconds, client_metadata=body.client_metadata,
    )
    view, created = ingest.init_upload(user.id, user.diario_id, idempotency_key, meta, body.total_size,
                                       body.chunk_size, body.sha256)
    if not created:
        response.status_code = status.HTTP_200_OK
    return view


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    return ingest.get_upload(user.id, user.diario_id, upload_id)


@router.put("/uploads/{upload_id}/chunks/{index}")
async def put_chunk(upload_id: UUID, index: int, request: Request,
                    x_chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
                    user: CurrentUser = CurrentUserDep) -> dict:
    limit = get_settings().max_chunk_bytes
    buf = bytearray()
    async for part in request.stream():
        buf.extend(part)
        if len(buf) > limit:
            raise IngestError(413, "blocco_troppo_grande", f"Blocco oltre {limit} byte")
    return await run_in_threadpool(ingest.put_chunk, user.id, user.diario_id, upload_id, index, bytes(buf), x_chunk_sha256)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    return ingest.complete_upload(user.id, user.diario_id, upload_id)


@router.delete("/uploads/{upload_id}")
def cancel_upload(upload_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    return ingest.cancel_upload(user.id, user.diario_id, upload_id)


@router.post("/recordings", status_code=status.HTTP_201_CREATED)
async def upload_single(
    response: Response,
    file: UploadFile = File(...),
    source: str = Form("caricamento"),
    recorded_at: datetime | None = Form(None),
    recorded_at_source: str | None = Form(None),
    duration_seconds: float | None = Form(None),
    sha256: str | None = Form(None),
    user: CurrentUser = CurrentUserDep,
) -> dict:
    """Caricamento in un'unica richiesta, per file brevi o script.

    È idempotente per contenuto: lo stesso file restituisce la stessa registrazione.
    Per vocali lunghi da telefono usare /uploads (riprendibile).
    """
    s = get_settings()
    meta = RecordingMeta(source=source, mime_type=file.content_type or "application/octet-stream",
                         filename=file.filename, recorded_at=recorded_at,
                         recorded_at_source=recorded_at_source, duration_seconds=duration_seconds)
    ingest.validate_meta(meta)
    tmp, fh = ingest.new_spool_file()
    h = hashlib.sha256()
    size = 0
    try:
        with fh:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > s.max_upload_bytes:
                    raise IngestError(413, "dimensione_non_ammessa", "File troppo grande")
                h.update(block)
                fh.write(block)
        if size == 0:
            raise IngestError(422, "file_vuoto", "Il file è vuoto")
        digest = h.hexdigest()
        if sha256 and sha256.lower() != digest:
            raise IngestError(422, "impronta_non_corrispondente", "sha256 dichiarato diverso da quello ricevuto")
        result = await run_in_threadpool(ingest.ingest_single, user.id, user.diario_id, tmp, digest, size, meta)
    finally:
        tmp.unlink(missing_ok=True)
    if result["duplicato"]:
        response.status_code = status.HTTP_200_OK
    return result
