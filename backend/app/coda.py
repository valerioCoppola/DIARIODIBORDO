"""Rimettere una nota nella coda del worker, da una fase precisa.

    modo None        analisi completa (riorganizzazione degli appunti e collocazione)
    modo "colloca"   solo la collocazione: gli appunti restano quelli salvati
    modo "evolvi"    solo le schede dei temi e le cose fatte (dopo una correzione a mano
                     della collocazione, che non ha bisogno del modello)
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

FERMI = ("archiviazione_completata", "errore", "errore_definitivo")
DA_DOVE = {None: "trascrizione_completata", "colloca": "trascrizione_completata",
           "evolvi": "dati_strutturati_salvati"}


def rimetti_in_coda(conn: psycopg.Connection, owner: UUID, entry_id: UUID, modo: str | None, messaggio: str,
                    ritardo_ms: int = 0, payload_extra: dict | None = None, stato: str | None = None) -> bool:
    """Falso se la nota è in elaborazione proprio ora (il worker la sta già rifacendo)."""
    e = conn.execute("SELECT recording_id FROM entries WHERE id = %s AND deleted_at IS NULL", (entry_id,)).fetchone()
    if e is None:
        return False
    job = conn.execute(
        """SELECT id, status::text AS stato, payload,
                  (locked_at IS NOT NULL AND locked_at > now() - interval '10 minutes') AS occupato
             FROM ingestion_jobs WHERE entry_id = %s OR (recording_id IS NOT NULL AND recording_id = %s)
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (entry_id, e["recording_id"])).fetchone()
    if job is not None and (job["stato"] not in FERMI or job["occupato"]):
        return False
    stato = stato or DA_DOVE[modo]
    payload = {k: v for k, v in ((job["payload"] if job else None) or {}).items() if k not in ("modo", "glossario")}
    payload.update(payload_extra or {})
    if modo:
        payload["modo"] = modo
    quando = f"now() + interval '{int(ritardo_ms)} milliseconds'"
    if job is None:
        job_id = conn.execute(
            f"""INSERT INTO ingestion_jobs (owner_id, kind, idempotency_key, entry_id, recording_id, status,
                                            payload, next_attempt_at)
                VALUES (%s, 'rianalisi', %s, %s, %s, %s, %s, {quando}) RETURNING id""",
            (owner, f"rianalisi:{entry_id}:{uuid4()}", entry_id, e["recording_id"], stato, Jsonb(payload))
        ).fetchone()["id"]
        prima = "ricevuto"
    else:
        job_id, prima = job["id"], job["stato"]
        conn.execute(
            f"""UPDATE ingestion_jobs SET status = %s, failed_stage = NULL, payload = %s,
                       recording_id = coalesce(recording_id, %s), entry_id = coalesce(entry_id, %s),
                       attempts = 0, error_code = NULL, last_error = NULL, completed_at = NULL,
                       locked_at = NULL, locked_by = NULL, next_attempt_at = {quando}
                 WHERE id = %s""", (stato, Jsonb(payload), e["recording_id"], entry_id, job_id))
    conn.execute("UPDATE entries SET processing_status = %s WHERE id = %s", (stato, entry_id))
    conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                 "VALUES (%s, %s, %s, %s, %s)", (owner, job_id, prima, stato, messaggio[:1000]))
    return True
