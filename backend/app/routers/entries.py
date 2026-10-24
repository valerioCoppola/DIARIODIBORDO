"""Note elaborate: elenco, dettaglio, esportazione, ricerca, azioni, riprova dei job."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import search as search_mod
from ..archivio import delete_recording
from ..correzione import Conflitto, correggi_trascrizione, sha
from ..conoscenza import percorso, pulisci_orfani, tinte
from ..db import user_tx
from ..evoluzione import prima_di_eliminare
from ..playback import remove_files
from ..security import CurrentUser, CurrentUserDep
from ..voci import etichetta, numero_voci
from .archive import with_original_offset
from .chiarimenti import domande
from .grafo import leggi_appunti, secondi

router = APIRouter(prefix="/api/v1", tags=["note"])


def _entry_summary(e: dict) -> dict:
    return {
        "id": e["id"], "titolo": e["titolo"], "categoria": e["categoria"],
        "data_ora": with_original_offset(e["data_ora"], e["data_ora_offset_minutes"]),
        "creato_il": e["created_at"], "executive_summary": e["executive_summary"],
        "stato": e["processing_status"], "recording_id": e["recording_id"], "versione": e["current_version"],
    }


_ENTRY_COLS = ("e.id, e.titolo, e.categoria::text AS categoria, e.data_ora, e.data_ora_offset_minutes, "
               "e.created_at, e.executive_summary, e.processing_status::text AS processing_status, "
               "e.recording_id, e.current_version")


@router.get("/entries")
def list_entries(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 categoria: str | None = None, user: CurrentUser = CurrentUserDep) -> dict:
    where = "e.deleted_at IS NULL" + (" AND e.categoria::text = %(cat)s" if categoria else "")
    with user_tx(user.id, user.diario_id) as conn:
        rows = conn.execute(f"SELECT {_ENTRY_COLS} FROM entries e WHERE {where} "
                            "ORDER BY coalesce(e.data_ora, e.created_at) DESC LIMIT %(l)s OFFSET %(o)s",
                            {"cat": categoria, "l": limit, "o": offset}).fetchall()
        total = conn.execute(f"SELECT count(*) AS n FROM entries e WHERE {where}", {"cat": categoria}).fetchone()["n"]
    return {"totale": total, "elementi": [_entry_summary(r) for r in rows]}


@router.get("/entries/{entry_id}")
def get_entry(entry_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        e = conn.execute(f"SELECT {_ENTRY_COLS}, e.trascrizione_grezza, e.source::text AS source, e.voci_nomi, "
                         "r.duration_seconds, r.mime_type, r.storage_status FROM entries e "
                         "LEFT JOIN recordings r ON r.id = e.recording_id "
                         "WHERE e.id = %s AND e.deleted_at IS NULL", (entry_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Nota non trovata")
        version = None
        if e["current_version"]:
            version = conn.execute("SELECT version, note_json, model, prompt_version, pipeline_version, warnings, "
                                   "created_at FROM entry_versions WHERE entry_id = %s AND version = %s",
                                   (entry_id, e["current_version"])).fetchone()
        actions = conn.execute(
            """SELECT a.id, a.descrizione, a.responsabile, a.scadenza, a.stato, a.quote, a.char_start, a.char_end,
                      a.topic_id,
                      (SELECT count(*) FROM action_item_events ev
                        WHERE ev.action_item_id = a.id AND ev.event = 'ribadita') AS ribadita,
                      (SELECT o.id || '|' || coalesce(o.titolo, '') FROM action_item_events ev
                         JOIN entries o ON o.id = ev.source_entry_id
                        WHERE ev.action_item_id = a.id AND ev.origin = 'estratto_automatico'
                          AND ev.event IN ('completata', 'annullata') AND a.stato <> 'aperta'
                        ORDER BY ev.created_at DESC LIMIT 1) AS chiusa_da
                 FROM action_items a WHERE a.entry_id = %s ORDER BY a.ord NULLS LAST, a.created_at""",
            (entry_id,)).fetchall()
        # Cose da fare di altri vocali che questo vocale ha chiuso o ripetuto.
        altre = conn.execute(
            """SELECT DISTINCT ON (a.id) a.id, a.descrizione, a.stato, ev.event,
                      o.id AS nota_id, o.titolo AS nota_titolo
                 FROM action_item_events ev
                 JOIN action_items a ON a.id = ev.action_item_id
                 JOIN entries o ON o.id = a.entry_id AND o.deleted_at IS NULL
                WHERE ev.source_entry_id = %s AND a.entry_id <> %s
                  AND ev.event IN ('completata', 'annullata', 'ribadita')
                ORDER BY a.id, ev.created_at DESC""", (entry_id, entry_id)).fetchall()
        concetti = conn.execute(
            "SELECT DISTINCT t.id, t.label, t.provvisorio FROM entry_topics et "
            "JOIN topics x ON x.id = et.topic_id JOIN topics t ON t.id = coalesce(x.merged_into, x.id) "
            "WHERE et.entry_id = %s ORDER BY t.label", (entry_id,)).fetchall()
        colori = tinte(conn) if concetti else {}
        cammini = {c["id"]: [x["nome"] for x in percorso(conn, c["id"])[:-1]] for c in concetti}
        da_chiarire = domande(conn, entry_id)
        tr = conn.execute("SELECT model, device, language, audio_seconds, segments, voci FROM transcriptions "
                          "WHERE entry_id = %s ORDER BY created_at DESC LIMIT 1", (entry_id,)).fetchone()
        appunti = leggi_appunti(conn, [entry_id], None, {entry_id: tr["segments"] if tr else []}).get(entry_id, [])
        job = conn.execute("SELECT id, status::text AS stato, error_code, last_error, attempts, next_attempt_at "
                           "FROM ingestion_jobs WHERE entry_id = %s OR (recording_id IS NOT NULL AND recording_id = %s) "
                           "ORDER BY created_at DESC LIMIT 1", (entry_id, e["recording_id"])).fetchone()
    return {
        **_entry_summary(e),
        "tag": (version["note_json"].get("tag", []) if version else []),
        "concetti": [{"id": c["id"], "nome": c["label"], "tinta": colori.get(c["id"], 0),
                      "percorso": cammini[c["id"]], "provvisorio": c["provvisorio"]} for c in concetti],
        "chiarimenti": da_chiarire,
        "voci": {"nomi": e["voci_nomi"] or {}, "persone": numero_voci(tr["segments"]) if tr else 0,
                 "stato": ((tr["voci"] or {}).get("stato") if tr else None)},
        "provenienza": e["source"],
        "trascrizione_grezza": e["trascrizione_grezza"],
        "trascrizione_sha": sha(e["trascrizione_grezza"] or ""),
        "segmenti": tr["segments"] if tr else [],
        "trascrizione": {"modello": tr["model"], "dispositivo": tr["device"], "lingua": tr["language"]} if tr else None,
        "durata_secondi": float(e["duration_seconds"]) if e["duration_seconds"] is not None else None,
        "audio_disponibile": e["storage_status"] in ("conservato", "verificato"),
        "action_items": [{"id": a["id"], "descrizione": a["descrizione"], "responsabile": a["responsabile"],
                          "scadenza": a["scadenza"], "stato": a["stato"], "quote": a["quote"], "fonte": a["quote"],
                          "char_start": a["char_start"], "char_end": a["char_end"], "tema": a["topic_id"],
                          "ribadita": a["ribadita"],
                          "secondi": secondi(tr["segments"] if tr else None, a["char_start"]),
                          "chiusa_da": dict(zip(("id", "titolo"), a["chiusa_da"].split("|", 1)))
                          if a["chiusa_da"] else None} for a in actions],
        "appunti": appunti,
        "altre_azioni": [{"id": a["id"], "descrizione": a["descrizione"], "stato": a["stato"],
                          "effetto": "ribadita" if a["event"] == "ribadita" else "chiusa",
                          "nota": {"id": a["nota_id"], "titolo": a["nota_titolo"]}} for a in altre],
        "analisi": {"versione": version["version"], "modello": version["model"], "prompt": version["prompt_version"],
                    "pipeline": version["pipeline_version"], "avvisi": version["warnings"],
                    "il": version["created_at"]} if version else None,
        "job": {"id": job["id"], "stato": job["stato"], "codice_errore": job["error_code"],
                "errore": job["last_error"], "tentativi": job["attempts"],
                "prossimo_tentativo": job["next_attempt_at"]} if job else None,
    }


@router.get("/entries/{entry_id}/nota.json")
def export_entry(entry_id: UUID, user: CurrentUser = CurrentUserDep) -> JSONResponse:
    """La nota nel formato canonico (spec. §3), così come è stata validata e salvata."""
    with user_tx(user.id, user.diario_id) as conn:
        v = conn.execute("SELECT v.note_json, e.titolo FROM entries e JOIN entry_versions v "
                         "ON v.entry_id = e.id AND v.version = e.current_version "
                         "WHERE e.id = %s AND e.deleted_at IS NULL", (entry_id,)).fetchone()
    if v is None:
        raise HTTPException(404, "Nota non ancora disponibile")
    return JSONResponse(v["note_json"], headers={
        "Content-Disposition": f'attachment; filename="nota-{entry_id}.json"'})


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=500), limit: int = Query(20, ge=1, le=50),
                 ramo: UUID | None = None, dal: datetime | None = None,
                 user: CurrentUser = CurrentUserDep) -> dict:
    def run() -> dict:
        with user_tx(user.id, user.diario_id) as conn:
            return search_mod.search(conn, q.strip(), limit, ramo, dal)
    return await run_in_threadpool(run)


@router.delete("/entries/{entry_id}")
def remove_entry(entry_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    """Elimina la nota insieme al vocale da cui viene (e a tutto ciò che ne deriva)."""
    path = None
    with user_tx(user.id, user.diario_id) as conn:
        e = conn.execute("SELECT recording_id FROM entries WHERE id = %s", (entry_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Nota non trovata")
        if e["recording_id"]:
            path = delete_recording(conn, e["recording_id"])
        else:
            jobs = [r["id"] for r in conn.execute("SELECT id FROM ingestion_jobs WHERE entry_id = %s",
                                                  (entry_id,)).fetchall()]
            prima_di_eliminare(conn, [entry_id])
            conn.execute("DELETE FROM model_usage WHERE job_id = ANY(%s)", (jobs,))
            conn.execute("DELETE FROM ingestion_jobs WHERE id = ANY(%s)", (jobs,))
            conn.execute("DELETE FROM entries WHERE id = %s", (entry_id,))
            pulisci_orfani(conn)
    if path is not None:
        remove_files(path)
    return {"id": entry_id, "eliminato": True}


class TrascrizioneIn(BaseModel):
    testo: str = Field(min_length=1, max_length=500_000)
    base: str | None = Field(default=None, description="trascrizione_sha del testo da cui parte la correzione")


@router.put("/entries/{entry_id}/trascrizione")
def correct_transcript(entry_id: UUID, body: TrascrizioneIn, user: CurrentUser = CurrentUserDep) -> dict:
    """Corregge a mano la trascrizione; sintesi, cose da fare e indice si rifanno sul testo nuovo."""
    if not body.testo.strip():
        raise HTTPException(422, {"errore": "testo_vuoto", "messaggio": "La trascrizione non può essere vuota"})
    try:
        with user_tx(user.id, user.diario_id) as conn:
            return correggi_trascrizione(conn, user.id, entry_id, body.testo, body.base)
    except LookupError:
        raise HTTPException(404, "Nota non trovata") from None
    except Conflitto as c:
        raise HTTPException(409, {"errore": c.codice, "messaggio": c.messaggio}) from None


class VociIn(BaseModel):
    nomi: dict[str, str] = Field(default_factory=dict, description='{"1": "Marco"}: nome vuoto = "Voce 1"')


@router.put("/entries/{entry_id}/voci")
def name_voices(entry_id: UUID, body: VociIn, user: CurrentUser = CurrentUserDep) -> dict:
    """Dà un nome alle voci di una nota. Le cose da fare assegnate a una voce seguono il nome."""
    nuovi: dict[str, str] = {}
    for k, v in body.nomi.items():
        if not k.isdigit() or not 1 <= int(k) <= 50:
            raise HTTPException(422, {"errore": "voce_non_valida", "messaggio": f"Voce sconosciuta: {k}"})
        v = " ".join(str(v).split())[:40]
        if v and not v.lower().startswith("voce "):
            nuovi[str(int(k))] = v
    with user_tx(user.id, user.diario_id) as conn:
        e = conn.execute("SELECT voci_nomi FROM entries WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                         (entry_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Nota non trovata")
        vecchi = e["voci_nomi"] or {}
        uniti = {**vecchi, **{k: v for k, v in nuovi.items()}}
        for k in body.nomi:
            if str(int(k)) not in nuovi:
                uniti.pop(str(int(k)), None)
        for k in {str(int(x)) for x in body.nomi}:
            prima, dopo = etichetta(int(k), vecchi), etichetta(int(k), uniti)
            if prima != dopo:
                conn.execute("UPDATE action_items SET responsabile = %s WHERE entry_id = %s AND responsabile = %s",
                             (dopo, entry_id, prima))
        conn.execute("UPDATE entries SET voci_nomi = %s WHERE id = %s", (Jsonb(uniti), entry_id))
    return {"id": entry_id, "nomi": uniti}


class ActionPatch(BaseModel):
    stato: Literal["aperta", "completata", "annullata"]


@router.patch("/action-items/{item_id}")
def update_action(item_id: UUID, body: ActionPatch, user: CurrentUser = CurrentUserDep) -> dict:
    event = {"aperta": "riaperta", "completata": "completata", "annullata": "annullata"}[body.stato]
    with user_tx(user.id, user.diario_id) as conn:
        a = conn.execute("UPDATE action_items SET stato = %s WHERE id = %s RETURNING id, stato, entry_id",
                         (body.stato, item_id)).fetchone()
        if a is None:
            raise HTTPException(404, "Azione non trovata")
        conn.execute("INSERT INTO action_item_events (owner_id, action_item_id, event, origin) "
                     "VALUES (%s, %s, %s, 'manuale')", (user.id, item_id, event))
    return {"id": a["id"], "stato": a["stato"]}


@router.post("/jobs/{job_id}/riprova")
def retry_job(job_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    """Rimette in coda un job in errore: riprende dalla fase in cui si era fermato."""
    with user_tx(user.id, user.diario_id) as conn:
        j = conn.execute("SELECT status::text AS s FROM ingestion_jobs WHERE id = %s FOR UPDATE",
                         (job_id,)).fetchone()
        if j is None:
            raise HTTPException(404, "Job non trovato")
        if j["s"] not in ("errore", "errore_definitivo"):
            raise HTTPException(409, {"errore": "job_non_in_errore",
                                      "messaggio": "Il job non è in errore: non serve rimetterlo in coda"})
        conn.execute("UPDATE ingestion_jobs SET status = 'errore', attempts = 0, next_attempt_at = now(), "
                     "locked_at = NULL, locked_by = NULL WHERE id = %s", (job_id,))
        conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                     "VALUES (%s, %s, %s, 'errore', 'rimesso in coda a mano')", (user.id, job_id, j["s"]))
    return {"id": job_id, "stato": "errore", "in_coda": True}
