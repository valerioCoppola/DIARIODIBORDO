"""Operazioni sull'archivio: file spariti dal PC, eliminazione completa, giorni locali."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import UUID

import psycopg

from . import storage
from .config import get_settings

log = logging.getLogger("diario.archivio")

DEFAULT_TZ = "Europe/Rome"

# Espressione SQL dell'istante di un vocale: inizio registrazione se noto, altrimenti arrivo al PC.
QUANDO = "coalesce(r.recorded_at, r.created_at)"
# Un vocale è visibile se non è stato eliminato e, se l'audio non c'è più, ha almeno una nota.
VISIBILE = "(r.storage_status <> 'eliminato' AND (r.storage_status <> 'mancante' OR e.id IS NOT NULL))"


def resolve_tz(conn: psycopg.Connection, tz: str | None) -> str:
    """Fuso orario del telefono per raggruppare per giorno; se sconosciuto, Europe/Rome."""
    if tz and len(tz) <= 64:
        if conn.execute("SELECT 1 FROM pg_timezone_names WHERE name = %s", (tz,)).fetchone():
            return tz
    return DEFAULT_TZ


def _storage_readable() -> bool:
    root = get_settings().storage_dir
    return root.is_dir() and os.access(root, os.R_OK | os.X_OK)


def _exists(rel: str) -> bool:
    try:
        return os.path.isfile(storage.absolute_path(rel))
    except ValueError:
        return False


def reconcile_storage(conn: psycopg.Connection, owner: UUID) -> None:
    """Allinea il database ai file presenti sul PC.

    - Originale sparito: la registrazione diventa "mancante" e il suo job, se non
      concluso, viene annullato. Senza nota non compare più nell'archivio.
    - Originale ricomparso (es. ripristinato da un backup): torna "conservato" e un
      job annullato per questo motivo torna in coda.
    Se la cartella dello storage non è leggibile non si tocca nulla: meglio non
    dichiarare spariti file che forse ci sono.
    """
    if not _storage_readable():
        log.warning("storage non leggibile: verifica dei file saltata")
        return
    rows = conn.execute("SELECT id, storage_path, storage_status FROM recordings "
                        "WHERE storage_status IN ('conservato', 'verificato', 'mancante')").fetchall()
    for r in rows:
        present = _exists(r["storage_path"])
        if not present and r["storage_status"] != "mancante":
            conn.execute("UPDATE recordings SET storage_status = 'mancante' WHERE id = %s", (r["id"],))
            jobs = conn.execute(
                """UPDATE ingestion_jobs SET status = 'annullato', error_code = 'originale_mancante',
                          last_error = 'file originale non più presente sul PC', locked_at = NULL, locked_by = NULL
                    WHERE recording_id = %s
                      AND (status NOT IN ('archiviazione_completata', 'annullato', 'errore_definitivo')
                           OR (status = 'errore_definitivo' AND error_code = 'originale_mancante'))
                RETURNING id, owner_id""", (r["id"],)).fetchall()
            for j in jobs:
                conn.execute("INSERT INTO job_events (owner_id, job_id, to_status, message) "
                             "VALUES (%s, %s, 'annullato', 'file originale non più presente sul PC')",
                             (j["owner_id"], j["id"]))
        elif present and r["storage_status"] == "mancante":
            conn.execute("UPDATE recordings SET storage_status = 'conservato' WHERE id = %s", (r["id"],))
            jobs = conn.execute(
                """UPDATE ingestion_jobs SET status = 'errore', attempts = 0, next_attempt_at = now()
                    WHERE recording_id = %s AND status = 'annullato' AND error_code = 'originale_mancante'
                RETURNING id, owner_id""", (r["id"],)).fetchall()
            for j in jobs:
                conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                             "VALUES (%s, %s, 'annullato', 'errore', 'file originale di nuovo presente: in coda')",
                             (j["owner_id"], j["id"]))


def delete_recording(conn: psycopg.Connection, recording_id: UUID) -> Path | None:
    """Elimina un vocale e tutto ciò che ne deriva. Restituisce il file da cancellare.

    Nota, versioni, trascrizioni, segmenti indicizzati, punti, azioni e il loro
    storico, ciò che la nota aveva cambiato nelle altre, argomenti rimasti senza note, job con storico e consumi, sessioni di
    caricamento. I file si cancellano dopo il commit (vedi remove_files): se la
    transazione fallisce, l'audio resta.
    """
    rec = conn.execute("SELECT id, storage_path FROM recordings WHERE id = %s FOR UPDATE",
                       (recording_id,)).fetchone()
    if rec is None:
        return None
    entries = [r["id"] for r in conn.execute("SELECT id FROM entries WHERE recording_id = %s",
                                             (recording_id,)).fetchall()]
    jobs = [r["id"] for r in conn.execute(
        "SELECT id FROM ingestion_jobs WHERE recording_id = %s OR entry_id = ANY(%s)",
        (recording_id, entries)).fetchall()]
    # Ciò che queste note avevano fatto alle altre (cose chiuse, punti superati) si disfa.
    from .evoluzione import prima_di_eliminare

    prima_di_eliminare(conn, entries)
    conn.execute("DELETE FROM model_usage WHERE job_id = ANY(%s)", (jobs,))
    conn.execute("DELETE FROM ingestion_jobs WHERE id = ANY(%s)", (jobs,))          # + job_events
    conn.execute("DELETE FROM entries WHERE id = ANY(%s)", (entries,))              # + versioni, segmenti, azioni…
    conn.execute("DELETE FROM upload_sessions WHERE recording_id = %s", (recording_id,))
    conn.execute("DELETE FROM recordings WHERE id = %s", (recording_id,))           # + trascrizioni
    # Temi rimasti senza note.
    from .conoscenza import pulisci_orfani

    pulisci_orfani(conn)
    try:
        return storage.absolute_path(rec["storage_path"])
    except ValueError:
        return None
