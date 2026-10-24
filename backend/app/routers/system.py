"""Salute del servizio e schema canonico della nota."""

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter, Response, status

from ..config import get_settings
from ..db import system_tx
from ..note_schema import NOTE_SCHEMA_VERSION, json_schema
from ..security import CurrentUser, CurrentUserDep

router = APIRouter(prefix="/api/v1", tags=["sistema"])


@router.get("/health")
def health(response: Response) -> dict:
    """Pubblico: non espone contenuti, solo se i componenti rispondono."""
    checks: dict[str, bool] = {}
    try:
        with system_tx() as conn:
            conn.execute("SELECT 1")
            checks["database"] = True
    except Exception:
        checks["database"] = False
    s = get_settings()
    checks["storage"] = s.storage_dir.is_dir() and os.access(s.storage_dir, os.W_OK)
    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": ok, "controlli": checks}


@router.get("/system")
def system_info(user: CurrentUser = CurrentUserDep) -> dict:
    s = get_settings()
    with system_tx() as conn:
        pg = conn.execute("SHOW server_version").fetchone()["server_version"]
        vec = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    usage = shutil.disk_usage(s.storage_dir)
    return {
        "postgres": pg, "pgvector": vec["extversion"] if vec else None,
        "schema_nota": NOTE_SCHEMA_VERSION,
        "spazio_libero_byte": usage.free, "spazio_totale_byte": usage.total,
    }


@router.get("/note-schema")
def note_schema() -> dict:
    return json_schema()
