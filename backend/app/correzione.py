"""Correzione a mano della trascrizione.

Il testo corretto sostituisce quello della nota; quello di prima resta nelle
versioni (entry_versions). Le posizioni che puntano al testo — segmenti con i
tempi dell'audio, citazioni delle cose da fare, segmenti indicizzati — vengono
spostate seguendo le differenze tra i due testi, così "tocca una frase" continua
a portare al punto giusto. Poi il job torna in coda dall'analisi: sintesi, cose
da fare e indice si rifanno sul testo corretto, senza ritrascrivere.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from difflib import SequenceMatcher
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)

# Stati in cui il worker non sta lavorando sulla nota: si può correggere.
FERMI = ("archiviazione_completata", "errore", "errore_definitivo")


class Conflitto(Exception):
    def __init__(self, codice: str, messaggio: str) -> None:
        super().__init__(messaggio)
        self.codice = codice
        self.messaggio = messaggio


def sha(testo: str) -> str:
    return hashlib.sha256(testo.encode("utf-8")).hexdigest()


class Mappa:
    """Sposta una posizione del testo vecchio nel testo nuovo.

    Il confronto è fatto per parole (spazi e punteggiatura a parte), poi riportato
    ai caratteri. Dentro un tratto uguale la posizione si sposta esattamente; dentro
    un tratto cambiato in proporzione, così due confini non si incrociano mai. Una
    parola inserita esattamente su un confine va con ciò che segue.
    """

    def __init__(self, old: str, new: str) -> None:
        self.old_len, self.new_len = len(old), len(new)
        a = [m.group() for m in _TOKEN.finditer(old)]
        b = [m.group() for m in _TOKEN.finditer(new)]
        ca, cb = _prefix(a), _prefix(b)
        self.blocks = [(tag, ca[i1], ca[i2], cb[j1], cb[j2])
                       for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
        self._starts = [b[1] for b in self.blocks]

    def __call__(self, p: int) -> int:
        if p <= 0:
            return 0
        if p >= self.old_len:
            return self.new_len
        # Tutti i blocchi che toccano p: si prende la posizione più a sinistra
        # (prima di un eventuale inserimento proprio lì).
        best: int | None = None
        i = bisect.bisect_right(self._starts, p) - 1
        while i >= 0:
            tag, o1, o2, n1, n2 = self.blocks[i]
            if o2 < p:
                break
            if o1 < p < o2:
                return n1 + (p - o1 if tag == "equal" else round((p - o1) * (n2 - n1) / (o2 - o1)))
            cand = n1 if o1 == p else n2   # p su un confine del blocco
            best = cand if best is None else min(best, cand)
            i -= 1
        return best if best is not None else self.new_len


def _prefix(tokens: list[str]) -> list[int]:
    out = [0]
    for t in tokens:
        out.append(out[-1] + len(t))
    return out


def _span(m: Mappa, start: int | None, end: int | None) -> tuple[int, int] | None:
    if start is None or end is None:
        return None
    a, b = m(start), m(end)
    return (a, b) if b > a else None


def correggi_trascrizione(conn: psycopg.Connection, owner: UUID, entry_id: UUID, testo: str,
                          base: str | None) -> dict:
    """Salva il testo corretto e rimette la nota in elaborazione dall'analisi.

    `base` è l'impronta del testo da cui è partita la correzione: se nel frattempo
    è cambiato, la correzione non sovrascrive niente.
    """
    e = conn.execute("SELECT id, trascrizione_grezza, current_version, recording_id FROM entries "
                     "WHERE id = %s AND deleted_at IS NULL FOR UPDATE", (entry_id,)).fetchone()
    if e is None:
        raise LookupError(entry_id)
    old = e["trascrizione_grezza"] or ""
    if base is not None and base != sha(old):
        raise Conflitto("testo_cambiato", "La trascrizione è cambiata nel frattempo: ricarica la nota.")
    if testo == old:
        return {"id": entry_id, "modificata": False, "trascrizione_sha": sha(old)}

    job = conn.execute(
        """SELECT id, status::text AS stato, kind,
                  (locked_at IS NOT NULL AND locked_at > now() - interval '10 minutes') AS occupato
             FROM ingestion_jobs WHERE entry_id = %s OR (recording_id IS NOT NULL AND recording_id = %s)
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (entry_id, e["recording_id"])).fetchone()
    if job is not None and (job["stato"] not in FERMI or job["occupato"]):
        raise Conflitto("in_elaborazione", "La nota è in elaborazione: riprova tra poco.")

    m = Mappa(old, testo)

    # Segmenti con i tempi dell'audio (solo l'ultima trascrizione è quella in uso).
    tr = conn.execute("SELECT id, segments FROM transcriptions WHERE entry_id = %s "
                      "ORDER BY created_at DESC LIMIT 1", (entry_id,)).fetchone()
    if tr is not None:
        segs = []
        for s in tr["segments"]:
            sp = _span(m, s.get("char_start"), s.get("char_end"))
            if sp is not None:
                segs.append({**s, "char_start": sp[0], "char_end": sp[1]})
        conn.execute("UPDATE transcriptions SET segments = %s WHERE id = %s", (Jsonb(segs), tr["id"]))

    # Cose da fare: la citazione segue il testo. Quelle estratte e mai toccate si
    # rifanno comunque con la nuova analisi.
    for a in conn.execute("SELECT id, char_start, char_end FROM action_items WHERE entry_id = %s "
                          "AND char_start IS NOT NULL", (entry_id,)).fetchall():
        sp = _span(m, a["char_start"], a["char_end"])
        conn.execute("UPDATE action_items SET char_start = %s, char_end = %s, quote = %s WHERE id = %s",
                     (sp[0] if sp else None, sp[1] if sp else None, testo[sp[0]:sp[1]] if sp else None, a["id"]))

    # Punti: anche la loro citazione segue il testo (la nuova analisi li rifà comunque).
    for p in conn.execute("SELECT id, char_start, char_end FROM punti WHERE entry_id = %s "
                          "AND char_start IS NOT NULL", (entry_id,)).fetchall():
        sp = _span(m, p["char_start"], p["char_end"])
        conn.execute("UPDATE punti SET char_start = %s, char_end = %s, quote = %s WHERE id = %s",
                     (sp[0] if sp else None, sp[1] if sp else None, testo[sp[0]:sp[1]] if sp else None, p["id"]))

    # Segmenti indicizzati: restano trovabili finché l'indicizzazione non li rifà.
    for c in conn.execute("SELECT id, char_start, char_end FROM text_chunks WHERE entry_id = %s",
                          (entry_id,)).fetchall():
        sp = _span(m, c["char_start"], c["char_end"])
        if sp is None:
            conn.execute("DELETE FROM text_chunks WHERE id = %s", (c["id"],))
        else:
            conn.execute("UPDATE text_chunks SET char_start = %s, char_end = %s, content = %s WHERE id = %s",
                         (sp[0], sp[1], testo[sp[0]:sp[1]], c["id"]))

    # Nuova versione della nota con il testo corretto; quella di prima resta.
    version = e["current_version"]
    if version is not None:
        prev = conn.execute("SELECT note_json FROM entry_versions WHERE entry_id = %s AND version = %s",
                            (entry_id, version)).fetchone()
        nv = conn.execute("SELECT max(version) + 1 AS v FROM entry_versions WHERE entry_id = %s",
                          (entry_id,)).fetchone()["v"]
        conn.execute(
            """INSERT INTO entry_versions (owner_id, entry_id, version, note_json, origin, correction_note)
               VALUES (%s, %s, %s, %s, 'correzione_manuale', 'trascrizione corretta a mano')""",
            (owner, entry_id, nv, Jsonb({**prev["note_json"], "trascrizione_grezza": testo})))
        version = nv

    # La nuova analisi riparte sempre: anche per le note già rianalizzate o senza job.
    rianalisi = True
    if job is None:
        job = conn.execute(
            """INSERT INTO ingestion_jobs (owner_id, kind, idempotency_key, entry_id, recording_id, status)
               VALUES (%s, 'rianalisi', %s, %s, %s, 'trascrizione_completata')
               RETURNING id, 'ricevuto' AS stato, kind""",
            (owner, f"correzione:{entry_id}:{uuid4()}", entry_id, e["recording_id"])).fetchone()
    conn.execute("UPDATE entries SET trascrizione_grezza = %s, transcript_sha256 = %s, current_version = %s, "
                 "processing_status = CASE WHEN %s THEN 'trascrizione_completata'::stato_job "
                 "ELSE processing_status END WHERE id = %s",
                 (testo, hashlib.sha256(testo.encode("utf-8")).digest(), version, rianalisi, entry_id))

    if rianalisi:
        conn.execute(
            """UPDATE ingestion_jobs SET status = 'trascrizione_completata', failed_stage = NULL, attempts = 0,
                      payload = payload - 'modo',
                      error_code = NULL, last_error = NULL, completed_at = NULL, locked_at = NULL,
                      locked_by = NULL, next_attempt_at = now() WHERE id = %s""", (job["id"],))
        conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                     "VALUES (%s, %s, %s, 'trascrizione_completata', 'trascrizione corretta a mano: nuova analisi')",
                     (owner, job["id"], job["stato"]))
    return {"id": entry_id, "modificata": True, "trascrizione_sha": sha(testo), "rianalisi": rianalisi}
