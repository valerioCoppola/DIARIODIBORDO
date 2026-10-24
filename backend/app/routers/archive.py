"""Consultazione dell'archivio: vocali, calendario, ascolto, eliminazione; job e stato."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .. import storage
from ..archivio import QUANDO, VISIBILE, delete_recording, reconcile_storage, resolve_tz
from ..db import user_tx
from ..playback import MEDIA_TYPE, ensure_playback, remove_files
from ..security import CurrentUser, CurrentUserDep

router = APIRouter(prefix="/api/v1", tags=["archivio"])

TERMINAL = ("archiviazione_completata", "errore_definitivo", "annullato")


def with_original_offset(dt: datetime | None, offset_minutes: int | None) -> str | None:
    """ISO 8601 con l'offset in cui è stata fatta la registrazione (spec. §3)."""
    if dt is None:
        return None
    if offset_minutes is not None:
        dt = dt.astimezone(timezone(timedelta(minutes=offset_minutes)))
    return dt.isoformat()


def _recording_out(r: dict) -> dict:
    return {
        "id": r["id"],
        "provenienza": r["source"],
        "nome_file": r["original_filename"],
        "mime_type": r["mime_type"],
        "formato": r["format"],
        "dimensione": r["size_bytes"],
        "durata_secondi": float(r["duration_seconds"]) if r["duration_seconds"] is not None else None,
        "sha256": bytes(r["sha256"]).hex(),
        "registrato_il": with_original_offset(r["recorded_at"], r["recorded_utc_offset_minutes"]),
        "fonte_data": r["recorded_at_source"],
        "stato_conservazione": r["storage_status"],
        "audio_disponibile": r["storage_status"] in ("conservato", "verificato"),
        "creato_il": r["created_at"],
        "giorno": r["giorno"].isoformat() if r.get("giorno") else None,
        "job": {"id": r["job_id"], "stato": r["job_status"], "tentativi": r["job_attempts"],
                "errore": r["job_error_code"], "messaggio_errore": r.get("job_last_error")}
               if r.get("job_id") else None,
        "nota": {"id": r["entry_id"], "titolo": r["entry_titolo"], "categoria": r["entry_categoria"],
                 "executive_summary": r["entry_summary"]} if r.get("entry_id") else None,
    }


def _rec_select(tz_param: str = "%(tz)s") -> str:
    return f"""
SELECT r.*, ({QUANDO} AT TIME ZONE {tz_param})::date AS giorno,
       j.id AS job_id, j.status AS job_status, j.attempts AS job_attempts, j.error_code AS job_error_code,
       j.last_error AS job_last_error,
       e.id AS entry_id, e.titolo AS entry_titolo, e.categoria::text AS entry_categoria,
       e.executive_summary AS entry_summary
  FROM recordings r
  LEFT JOIN LATERAL (SELECT * FROM ingestion_jobs WHERE recording_id = r.id ORDER BY created_at LIMIT 1) j ON true
  LEFT JOIN entries e ON e.recording_id = r.id AND e.deleted_at IS NULL
"""


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@router.get("/recordings")
def list_recordings(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    giorno: str | None = Query(None, description="AAAA-MM-GG: solo i vocali di quel giorno"),
                    tz: str | None = Query(None, description="fuso del telefono, es. Europe/Rome"),
                    user: CurrentUser = CurrentUserDep) -> dict:
    """Vocali dal più recente. I vocali il cui audio è sparito dal PC, senza nota, non compaiono."""
    if giorno and not _DAY_RE.match(giorno):
        raise HTTPException(422, "giorno nel formato AAAA-MM-GG")
    with user_tx(user.id, user.diario_id) as conn:
        if offset == 0:
            reconcile_storage(conn, user.id)
        tzn = resolve_tz(conn, tz)
        where = VISIBILE + (f" AND ({QUANDO} AT TIME ZONE %(tz)s)::date = %(g)s::date" if giorno else "")
        params = {"tz": tzn, "g": giorno, "l": limit, "o": offset}
        rows = conn.execute(_rec_select() + f" WHERE {where} ORDER BY {QUANDO} DESC LIMIT %(l)s OFFSET %(o)s",
                            params).fetchall()
        total = conn.execute(f"SELECT count(*) AS n FROM recordings r LEFT JOIN entries e ON e.recording_id = r.id "
                             f"AND e.deleted_at IS NULL WHERE {where}", params).fetchone()["n"]
    return {"totale": total, "fuso": tzn, "elementi": [_recording_out(r) for r in rows]}


@router.get("/calendario")
def calendar(mese: str = Query(..., description="AAAA-MM"), tz: str | None = None,
             user: CurrentUser = CurrentUserDep) -> dict:
    """Quanti vocali per giorno nel mese, nel fuso del telefono."""
    if not _MONTH_RE.match(mese):
        raise HTTPException(422, "mese nel formato AAAA-MM")
    y, m = map(int, mese.split("-"))
    if not 1 <= m <= 12:
        raise HTTPException(422, "mese non valido")
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    with user_tx(user.id, user.diario_id) as conn:
        reconcile_storage(conn, user.id)
        tzn = resolve_tz(conn, tz)
        rows = conn.execute(
            f"""SELECT ({QUANDO} AT TIME ZONE %(tz)s)::date AS g, count(*) AS n,
                       count(*) FILTER (WHERE e.titolo IS NOT NULL) AS note,
                       array_remove(array_agg(DISTINCT e.categoria::text), NULL) AS categorie
                  FROM recordings r LEFT JOIN entries e ON e.recording_id = r.id AND e.deleted_at IS NULL
                 WHERE {VISIBILE} AND ({QUANDO} AT TIME ZONE %(tz)s)::date >= %(a)s
                   AND ({QUANDO} AT TIME ZONE %(tz)s)::date < %(b)s
                 GROUP BY 1""", {"tz": tzn, "a": first, "b": nxt}).fetchall()
        bounds = conn.execute(
            f"""SELECT min(({QUANDO} AT TIME ZONE %(tz)s)::date) AS primo, max(({QUANDO} AT TIME ZONE %(tz)s)::date) AS ultimo
                  FROM recordings r LEFT JOIN entries e ON e.recording_id = r.id AND e.deleted_at IS NULL
                 WHERE {VISIBILE}""", {"tz": tzn}).fetchone()
    return {
        "mese": mese, "fuso": tzn,
        "giorni": {r["g"].isoformat(): {"vocali": r["n"], "note": r["note"], "categorie": sorted(r["categorie"])}
                   for r in rows},
        "primo_giorno": bounds["primo"].isoformat() if bounds["primo"] else None,
        "ultimo_giorno": bounds["ultimo"].isoformat() if bounds["ultimo"] else None,
    }


@router.get("/recordings/{recording_id}")
def get_recording(recording_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        r = conn.execute(_rec_select("'UTC'") + " WHERE r.id = %s", (recording_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Registrazione non trovata")
    return _recording_out(r)


@router.get("/recordings/{recording_id}/audio")
async def get_recording_audio(recording_id: UUID, originale: bool = False,
                              user: CurrentUser = CurrentUserDep) -> FileResponse:
    """Per ascoltare: la copia MP3, che ogni browser riproduce e scorre.
    Con ?originale=1: il file così come è arrivato dal telefono, da scaricare."""
    with user_tx(user.id, user.diario_id) as conn:
        r = conn.execute("SELECT storage_path, mime_type, original_filename, recorded_at, created_at "
                         "FROM recordings WHERE id = %s", (recording_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Registrazione non trovata")
    path = storage.absolute_path(r["storage_path"])
    if not path.exists():
        raise HTTPException(410, "File originale non più presente sul PC")
    headers = {"Cache-Control": "private, no-store"}
    if originale:
        when = (r["recorded_at"] or r["created_at"]).strftime("%Y-%m-%d_%H-%M")
        name = f"vocale_{when}.{storage.extension_for(r['mime_type'])}"
        return FileResponse(path, media_type=storage.base_mime(r["mime_type"]), headers=headers, filename=name)
    copy = await run_in_threadpool(ensure_playback, path)
    if copy is None:  # conversione non riuscita: si prova con l'originale
        return FileResponse(path, media_type=storage.base_mime(r["mime_type"]), headers=headers)
    return FileResponse(copy, media_type=MEDIA_TYPE, headers=headers)


@router.delete("/recordings/{recording_id}")
def remove_recording(recording_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    """Elimina il vocale dal Diario: audio, trascrizione, nota, cose da fare, indice, storico."""
    with user_tx(user.id, user.diario_id) as conn:
        found = conn.execute("SELECT 1 FROM recordings WHERE id = %s", (recording_id,)).fetchone()
        path = delete_recording(conn, recording_id) if found else None
    if not found:
        raise HTTPException(404, "Registrazione non trovata")
    if path is not None:
        remove_files(path)
    return {"id": recording_id, "eliminato": True}


def _job_out(j: dict) -> dict:
    return {
        "id": j["id"], "tipo": j["kind"], "stato": j["status"], "fase_errore": j["failed_stage"],
        "tentativi": j["attempts"], "tentativi_massimi": j["max_attempts"],
        "codice_errore": j["error_code"], "errore": j["last_error"],
        "recording_id": j["recording_id"], "entry_id": j["entry_id"],
        "prossimo_tentativo": j["next_attempt_at"], "creato_il": j["created_at"],
        "aggiornato_il": j["updated_at"], "completato_il": j["completed_at"],
        "concluso": j["status"] in TERMINAL,
    }


@router.get("/jobs")
def list_jobs(stato: str | None = None, limit: int = Query(50, ge=1, le=200),
              user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        if stato:
            rows = conn.execute("SELECT * FROM ingestion_jobs WHERE status::text = %s "
                                "ORDER BY created_at DESC LIMIT %s", (stato, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ingestion_jobs ORDER BY created_at DESC LIMIT %s",
                                (limit,)).fetchall()
    return {"elementi": [_job_out(j) for j in rows]}


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        j = conn.execute("SELECT * FROM ingestion_jobs WHERE id = %s", (job_id,)).fetchone()
        if j is None:
            raise HTTPException(404, "Job non trovato")
        events = conn.execute("SELECT from_status, to_status, message, created_at FROM job_events "
                              "WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
    return {**_job_out(j), "eventi": [
        {"da": e["from_status"], "a": e["to_status"], "messaggio": e["message"], "il": e["created_at"]}
        for e in events]}


@router.get("/status")
def archive_status(user: CurrentUser = CurrentUserDep) -> dict:
    """Riepilogo: vocali e note visibili, audio, job per stato, elaborazione attiva."""
    with user_tx(user.id, user.diario_id) as conn:
        jobs = conn.execute("SELECT status::text AS s, count(*) AS n FROM ingestion_jobs GROUP BY 1").fetchall()
        rec = conn.execute(f"SELECT count(*) AS n, coalesce(sum(r.size_bytes), 0) AS bytes, "
                           f"coalesce(sum(r.duration_seconds), 0) AS secs FROM recordings r "
                           f"LEFT JOIN entries e ON e.recording_id = r.id AND e.deleted_at IS NULL "
                           f"WHERE {VISIBILE}").fetchone()
        entries = conn.execute("SELECT count(*) AS n FROM entries WHERE deleted_at IS NULL").fetchone()
        open_uploads = conn.execute("SELECT count(*) AS n FROM upload_sessions WHERE status = 'aperta'").fetchone()
        worker = conn.execute("SELECT last_seen_at, info, now() - last_seen_at < interval '90 seconds' AS attivo "
                              "FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 1").fetchone()
    pending = sum(r["n"] for r in jobs if r["s"] not in TERMINAL)
    return {
        "elaboratore": {"attivo": bool(worker and worker["attivo"]),
                        "visto_il": worker["last_seen_at"] if worker else None,
                        "dettagli": worker["info"] if worker else None},
        "in_elaborazione": pending,
        "job_per_stato": {r["s"]: r["n"] for r in jobs},
        "registrazioni": rec["n"], "byte_audio": int(rec["bytes"]), "secondi_audio": float(rec["secs"]),
        "note": entries["n"], "caricamenti_aperti": open_uploads["n"],
    }
