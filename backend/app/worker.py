"""Worker dell'elaborazione: preleva i job dalla coda e li porta fino all'archivio.

    python -m app.worker            (avviato da scripts/worker.sh / servizio diario-worker)

Fasi (spec. §9), ciascuna registrata in ingestion_jobs e job_events:

    originale_conservato
      → trascrizione_in_corso → trascrizione_completata      (faster-whisper)
      → analisi_in_corso      → dati_strutturati_salvati     (Ollama, validato contro Nota)
      →                         indicizzazione_completata    (segmenti + embedding)
      →                         archiviazione_completata

Ogni fase salva il suo risultato nella stessa transazione in cui cambia stato:
se qualcosa si rompe, il job riparte dalla fase fallita senza rifare le
precedenti. Errori:
  - servizio non disponibile (Ollama spento, modello non scaricato): riprova
    ogni minuto, senza consumare tentativi;
  - errore definitivo (audio senza parlato, file mancante): si ferma subito;
  - altri errori: riprova con attesa crescente (30 s, 1, 2, 4… min), poi si ferma
    dopo max_attempts. Dall'interfaccia si può sempre rimettere in coda.

Il job preso è "affittato" e il worker rinnova l'affitto con un battito: se il
processo muore, dopo worker_lease_seconds il job torna prelevabile e riprende.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta, timezone
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from . import storage
from .playback import ensure_playback
from .config import Settings, get_settings
from .db import close_pool, system_tx, user_tx
from .note_schema import ActionItem, Nota, soft_warnings
from .pipeline import PIPELINE_VERSION, Pipeline, build_pipeline
from .pipeline.analyze import PROMPT_VERSION
from .pipeline.chunking import chunk_text
from .pipeline.errors import PermanentError, PipelineError, ServiceUnavailable
from . import evoluzione as evo
from .conoscenza import (Soglie, albero_senza, applica_riordino, carica_albero, carica_blocchi, confermati, conta_per_riordino, da_riordinare,
                         indicazioni, nomi_da_vettorizzare, salva_collocazione, salva_embedding_temi, segna_riordino,
                         senza_embedding, valuta)
from .note_schema import Categoria
from .pipeline.analyze import AnalysisOutcome, Contesto, testo_blocco
from .pipeline.colloca import PROMPT_COLLOCA, albero_testo
from .pipeline.text import Span, best_supporting_sentence
from .voci import assegna_voci, etichette, numero_voci, testo_con_voci

log = logging.getLogger("diario.worker")

ORDER = ["trascrizione", "analisi", "indicizzazione", "archiviazione"]
RESUME = {
    "ricevuto": "trascrizione",
    "originale_conservato": "trascrizione",
    "trascrizione_in_corso": "trascrizione",
    "trascrizione_completata": "analisi",
    "analisi_in_corso": "analisi",
    "dati_strutturati_salvati": "indicizzazione",
    "indicizzazione_completata": "archiviazione",
}


class JobGone(Exception):
    """Il job non esiste più: il vocale è stato eliminato durante l'elaborazione."""


def _safe_message(e: BaseException) -> str:
    """Solo messaggi tecnici, brevi: mai contenuti dei vocali."""
    return f"{e}"[:500] if isinstance(e, PipelineError) else f"{type(e).__name__}: {e}"[:500]


def _vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _TAG_OK(t: str) -> bool:
    import re

    return bool(re.match(r"^[\wàèéìíòóùú][\wàèéìíòóùú' -]{0,39}$", t, re.IGNORECASE))


def campione_passaggi(testo: str, n: int = 6):
    """Alcuni passaggi distribuiti nel vocale: bastano per capire di quali temi parla."""
    chunks = chunk_text(testo)
    if len(chunks) <= n:
        return chunks
    passo = len(chunks) / n
    return [chunks[int(i * passo)] for i in range(n)]


class Worker:
    def __init__(self, pipeline: Pipeline, settings: Settings | None = None, worker_id: str | None = None) -> None:
        self.p = pipeline
        self.s = settings or get_settings()
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stop = threading.Event()
        self._job: tuple[UUID, UUID, UUID] | None = None   # (job, proprietario, diario) in lavorazione
        self._diario: UUID | None = None             # diario del job: ogni transazione lavora lì
        self._stage: str | None = None               # ultimo stato raggiunto dal job
        self._resumed_status: str | None = None      # stato del job quando è stato preso
        self._events_mark = 0

    # -- ciclo principale ----------------------------------------------------------

    def run_forever(self) -> None:
        log.info("worker %s avviato: %s", self.id, self.p.describe())
        beat = threading.Thread(target=self._heartbeat_loop, name="battito", daemon=True)
        beat.start()
        listen = psycopg.connect(self.s.database_url, autocommit=True)
        try:
            listen.execute("LISTEN diario_jobs")
            while not self.stop.is_set():
                try:
                    if self.run_once():
                        continue
                except psycopg.OperationalError as e:
                    log.warning("database non raggiungibile (%s): riprovo", e)
                    self.stop.wait(5)
                    continue
                # Niente da fare: aspetta una notifica o il prossimo controllo.
                for _ in listen.notifies(timeout=self.s.worker_poll_seconds, stop_after=1):
                    pass
        finally:
            listen.close()
            self.stop.set()

    def run_once(self) -> bool:
        """Elabora un job, se ce n'è uno pronto. Vero se ne ha preso uno."""
        claimed = self.claim()
        if claimed is None:
            return False
        self.process(*claimed)
        return True

    def claim(self) -> tuple[UUID, UUID, UUID] | None:
        with system_tx() as conn:
            row = conn.execute("SELECT o_job_id, o_owner_id, o_diario_id FROM claim_next_job(%s, %s)",
                               (self.id, self.s.worker_lease_seconds)).fetchone()
        return (row["o_job_id"], row["o_owner_id"], row["o_diario_id"]) if row else None

    # -- battito ---------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(self.s.worker_heartbeat_seconds):
            try:
                self.beat()
            except Exception as e:  # il battito non deve mai fermare il worker
                log.warning("battito non registrato: %s", e)

    def beat(self) -> None:
        with system_tx() as conn:
            conn.execute(
                """INSERT INTO worker_heartbeats (worker_id, info) VALUES (%s, %s)
                   ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now(), info = EXCLUDED.info""",
                (self.id, Jsonb({**self.p.describe(), "in_lavorazione": self._job is not None})))
            conn.execute("DELETE FROM worker_heartbeats WHERE last_seen_at < now() - interval '1 day'")
        job = self._job
        if job:
            with user_tx(job[1], job[2]) as conn:
                conn.execute("UPDATE ingestion_jobs SET locked_at = now() WHERE id = %s AND locked_by = %s",
                             (job[0], self.id))

    # -- un job --------------------------------------------------------------------

    def process(self, job_id: UUID, owner: UUID, diario: UUID) -> None:
        self._diario = diario
        with user_tx(owner, self._diario) as conn:
            job = conn.execute("SELECT * FROM ingestion_jobs WHERE id = %s", (job_id,)).fetchone()
        if job is None:
            return
        self._resumed_status = job["status"]
        start = job["failed_stage"] if job["status"] == "errore" else job["status"]
        self._stage = start
        self._job = (job_id, owner, diario)
        with user_tx(owner, self._diario) as conn:
            self._events_mark = conn.execute("SELECT coalesce(max(id), 0) AS m FROM job_events WHERE job_id = %s",
                                             (job_id,)).fetchone()["m"]
        self._versione_appunti = 0
        try:
            if job["kind"] not in ("audio", "rianalisi"):
                raise PermanentError(f"tipo di job non ancora gestito: {job['kind']}", code="tipo_non_gestito")
            step = RESUME.get(start or "", "trascrizione")
            for name in ORDER[ORDER.index(step):]:
                getattr(self, f"_step_{name}")(owner, job_id)
        except JobGone:
            log.info("job %s eliminato durante l'elaborazione: lascio perdere", job_id)
        except ServiceUnavailable as e:
            log.warning("job %s in attesa: %s", job_id, e)
            self._fail(owner, job_id, e, refund=True)
        except PermanentError as e:
            log.error("job %s fermato: %s", job_id, e)
            self._fail(owner, job_id, e, permanent=True)
        except Exception as e:
            log.exception("job %s: errore nella fase %s", job_id, self._stage)
            self._fail(owner, job_id, e)
        finally:
            self._job = None

    def _transition(self, conn: psycopg.Connection, owner: UUID, job_id: UUID, to: str,
                    msg: str | None = None, **extra: object) -> None:
        cur = conn.execute("SELECT status::text AS s, entry_id FROM ingestion_jobs WHERE id = %s FOR UPDATE",
                           (job_id,)).fetchone()
        if cur is None:
            raise JobGone(str(job_id))
        sets = ["status = %s"]
        params: list[object] = [to]
        for k, v in extra.items():
            sets.append(f"{k} = %s")
            params.append(v)
        conn.execute(f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE id = %s", (*params, job_id))
        entry_id = extra.get("entry_id", cur["entry_id"])
        if entry_id:
            conn.execute("UPDATE entries SET processing_status = %s WHERE id = %s", (to, entry_id))
        conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                     "VALUES (%s, %s, %s, %s, %s)", (owner, job_id, cur["s"], to, msg))
        self._stage = to

    def _usage(self, conn: psycopg.Connection, owner: UUID, job_id: UUID, model: str, op: str,
               inp: float | int | None, out: int | None, ms: int) -> None:
        conn.execute("INSERT INTO model_usage (owner_id, job_id, provider, model, operation, input_units, "
                     "output_units, duration_ms) VALUES (%s, %s, 'locale', %s, %s, %s, %s, %s)",
                     (owner, job_id, model, op, None if inp is None else int(round(inp)), out, ms))

    def _fail(self, owner: UUID, job_id: UUID, e: BaseException, refund: bool = False,
              permanent: bool = False) -> None:
        code = getattr(e, "code", None) or "errore_interno"
        msg = _safe_message(e)
        with user_tx(owner, self._diario) as conn:
            j = conn.execute("SELECT status::text AS s, attempts, max_attempts, error_code, entry_id "
                             "FROM ingestion_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
            if j is None:   # vocale eliminato dall'utente durante l'elaborazione
                log.info("job %s eliminato durante l'elaborazione", job_id)
                return
            attempts = j["attempts"]
            if refund:
                attempts = max(attempts - 1, 0)
                to, delay = "errore", self.s.worker_unavailable_retry_seconds
            elif code == "originale_mancante":
                to, delay = "annullato", 0
                conn.execute("UPDATE recordings SET storage_status = 'mancante' WHERE id = "
                             "(SELECT recording_id FROM ingestion_jobs WHERE id = %s)", (job_id,))
            elif permanent or attempts >= j["max_attempts"]:
                to, delay = "errore_definitivo", 0
            else:
                to, delay = "errore", min(30 * 2 ** max(attempts - 1, 0), self.s.worker_max_backoff_seconds)
            failed_stage = self._stage if self._stage not in (None, "errore", "errore_definitivo") else None
            conn.execute(
                """UPDATE ingestion_jobs SET status = %s, failed_stage = COALESCE(%s::stato_job, failed_stage),
                          attempts = %s, error_code = %s, last_error = %s,
                          next_attempt_at = now() + make_interval(secs => %s), locked_at = NULL, locked_by = NULL
                    WHERE id = %s""",
                (to, failed_stage, attempts, code, msg, delay, job_id))
            if j["entry_id"]:
                conn.execute("UPDATE entries SET processing_status = %s WHERE id = %s", (to, j["entry_id"]))
            # Un servizio spento produce un errore al minuto: basta registrarlo la prima volta,
            # senza nemmeno le ripartenze che lo hanno ritrovato spento.
            repeated = refund and j["error_code"] == code and self._resumed_status == "errore"
            if repeated:
                conn.execute("DELETE FROM job_events WHERE job_id = %s AND id > %s", (job_id, self._events_mark))
            else:
                note = " (nuovo tentativo appena il servizio risponde)" if refund else (
                    "" if to in ("errore_definitivo", "annullato") else f" (nuovo tentativo tra {delay} s)")
                conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                             "VALUES (%s, %s, %s, %s, %s)",
                             (owner, job_id, j["s"], to, f"{code}: {msg}{note}"[:1000]))

    # -- fasi --------------------------------------------------------------------------

    def _step_trascrizione(self, owner: UUID, job_id: UUID) -> None:
        with user_tx(owner, self._diario) as conn:
            rec = conn.execute("SELECT r.*, j.payload AS job_payload FROM ingestion_jobs j "
                               "JOIN recordings r ON r.id = j.recording_id WHERE j.id = %s", (job_id,)).fetchone()
            if rec is None:
                raise PermanentError("il job non ha una registrazione", code="registrazione_mancante")
            self._transition(conn, owner, job_id, "trascrizione_in_corso")
        path = storage.absolute_path(rec["storage_path"])
        if not path.is_file():
            raise PermanentError("file originale non più presente sul PC", code="originale_mancante")
        ensure_playback(path)   # copia d'ascolto MP3; se non riesce si ascolta l'originale
        self._libera_gpu()
        glossario = ""
        if self.s.whisper_glossario:
            with user_tx(owner, self._diario) as conn:
                glossario = evo.glossario(conn)
            # Ritrascrizione di tutto l'archivio: il glossario preso prima di ripartire da capo.
            salvato = (rec["job_payload"] or {}).get("glossario") or ""
            if salvato:
                glossario = evo.unisci_glossari(salvato, glossario)

        res = self.p.transcriber.transcribe(path, glossario=glossario or None)
        if not res.text.strip():
            raise PermanentError("nessun parlato riconosciuto nell'audio", code="nessun_parlato")

        import hashlib

        sha = hashlib.sha256(res.text.encode("utf-8")).digest()
        with user_tx(owner, self._diario) as conn:
            entry = conn.execute(
                """INSERT INTO entries (owner_id, recording_id, data_ora, data_ora_offset_minutes, source,
                                        trascrizione_grezza, transcript_sha256, processing_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'trascrizione_completata')
                   ON CONFLICT (recording_id) WHERE recording_id IS NOT NULL AND deleted_at IS NULL
                   DO UPDATE SET trascrizione_grezza = EXCLUDED.trascrizione_grezza,
                                 transcript_sha256 = EXCLUDED.transcript_sha256
                   RETURNING id""",
                (owner, rec["id"], rec["recorded_at"], rec["recorded_utc_offset_minutes"], rec["source"],
                 res.text, sha)).fetchone()
            # Nuova trascrizione di una nota esistente: le cose da fare tenute (spuntate o toccate)
            # ritrovano la loro frase nel testo nuovo.
            for a in conn.execute("SELECT id, quote FROM action_items WHERE entry_id = %s AND quote IS NOT NULL",
                                  (entry["id"],)).fetchall():
                sp = best_supporting_sentence(a["quote"], res.text, 0.5)
                conn.execute("UPDATE action_items SET char_start = %s, char_end = %s, quote = %s WHERE id = %s",
                             (sp.start if sp else None, sp.end if sp else None,
                              sp.text(res.text) if sp else a["quote"], a["id"]))
            conn.execute(
                """INSERT INTO transcriptions (owner_id, recording_id, entry_id, model, device, language,
                                               language_probability, audio_seconds, speech_seconds, segments)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (owner, rec["id"], entry["id"], res.model, res.device, res.language, res.language_probability,
                 res.audio_seconds, res.speech_seconds, Jsonb(res.segments)))
            if res.audio_seconds and rec["duration_seconds"] is None:
                conn.execute("UPDATE recordings SET duration_seconds = %s WHERE id = %s",
                             (round(res.audio_seconds, 3), rec["id"]))
            self._usage(conn, owner, job_id, res.model, "trascrizione", res.audio_seconds, len(res.text),
                        res.elapsed_ms)
            secs = f"{res.audio_seconds:.0f} s di audio" if res.audio_seconds else "audio"
            self._transition(conn, owner, job_id, "trascrizione_completata",
                             f"{res.model} su {res.device} ({res.compute_type}): {secs} in "
                             f"{res.elapsed_ms / 1000:.0f} s", entry_id=entry["id"])

    def _libera_gpu(self) -> None:
        """Su una scheda video piccola Whisper e pyannote non convivono con il modello di analisi:
        prima di usarli si chiede a Ollama di scaricarlo (si ricarica da solo alla prossima analisi)."""
        scelta = getattr(self.p.transcriber, "choice", None)
        client = getattr(self.p.analyzer, "client", None)
        if scelta is None or client is None or scelta.device != "cuda" or scelta.keep_loaded:
            return
        try:
            for m in client.loaded():
                if m.get("size_vram"):
                    client.unload(m.get("name", ""))
        except Exception as e:      # Ollama spento: niente da liberare
            log.debug("scheda video non liberata: %s", e)

    def _assicura_voci(self, owner: UUID, entry: dict) -> None:
        """Chi parla in ogni segmento, se non è ancora stato riconosciuto (anche su trascrizioni vecchie).

        Non blocca mai la nota: se il riconoscimento non è preparato o non riesce, l'analisi
        procede senza voci e l'esito resta registrato nella trascrizione.
        """
        with user_tx(owner, self._diario) as conn:
            tr = conn.execute("""SELECT t.id, t.segments, t.voci, r.storage_path FROM transcriptions t
                                   JOIN recordings r ON r.id = t.recording_id
                                  WHERE t.entry_id = %s ORDER BY t.created_at DESC LIMIT 1""",
                              (entry["id"],)).fetchone()
        if tr is None or (tr["voci"] or {}).get("stato") or not tr["segments"]:
            return
        persone = (tr["voci"] or {}).get("persone")
        esito: dict
        segs = tr["segments"]
        if self.p.voci is None or not self.p.voci.disponibile():
            esito = {"stato": "non_preparato"}
        else:
            try:
                path = storage.absolute_path(tr["storage_path"])
                if not path.is_file():
                    raise PipelineError("file originale non più presente sul PC", code="originale_mancante")
                self._libera_gpu()
                r = self.p.voci.diarizza(path, persone)
                segs, n = assegna_voci(segs, r.turni)
                esito = {"stato": "ok", "persone": n, "dispositivo": r.dispositivo, "secondi": round(r.secondi)}
                log.info("voci riconosciute: %d in %.0f s su %s", n, r.secondi, r.dispositivo)
            except Exception as e:   # mai bloccare la nota per le voci
                log.warning("riconoscimento delle voci non riuscito: %s", _safe_message(e))
                esito = {"stato": "errore", "messaggio": _safe_message(e)[:300]}
        with user_tx(owner, self._diario) as conn:
            conn.execute("UPDATE transcriptions SET segments = %s, voci = %s WHERE id = %s",
                         (Jsonb(segs), Jsonb(esito), tr["id"]))

    def _embed(self, testi: list[str]) -> tuple[list[list[float]], str, int]:
        if not testi:
            return [], self.p.embedder.model, 0
        return self.p.embedder.embed(testi)

    def _step_analisi(self, owner: UUID, job_id: UUID) -> None:
        with user_tx(owner, self._diario) as conn:
            entry = conn.execute("SELECT e.*, j.payload AS job_payload FROM ingestion_jobs j "
                                 "JOIN entries e ON e.id = j.entry_id WHERE j.id = %s", (job_id,)).fetchone()
            if entry is None:
                raise PipelineError("trascrizione non trovata per l'analisi", code="trascrizione_mancante")
            self._transition(conn, owner, job_id, "analisi_in_corso")
            salvati = carica_blocchi(conn, entry["id"]) if (entry["job_payload"] or {}).get("modo") == "colloca" \
                else None

        testo = entry["trascrizione_grezza"]
        if salvati:
            # Solo la collocazione: gli appunti restano quelli salvati.
            out = AnalysisOutcome(titolo=entry["titolo"] or "", categoria=Categoria(entry["categoria"] or "Altro"),
                                  blocchi=salvati, warnings=[], model=self.p.analyzer.model)
        else:
            self._assicura_voci(owner, entry)
            with user_tx(owner, self._diario) as conn:
                tr = conn.execute("SELECT segments FROM transcriptions WHERE entry_id = %s "
                                  "ORDER BY created_at DESC LIMIT 1", (entry["id"],)).fetchone()
                nomi = conn.execute("SELECT voci_nomi FROM entries WHERE id = %s",
                                    (entry["id"],)).fetchone()["voci_nomi"]
                segs = tr["segments"] if tr else []
            contesto = Contesto(dialogo=testo_con_voci(testo, segs, nomi),
                                voci=etichette(segs, nomi) if numero_voci(segs) > 1 else set())
            out = self.p.analyzer.analyze(testo, contesto)

        temi, avvisi_col, uso_col = self._colloca(owner, job_id, entry["id"], out, testo,
                                                  scrivi_appunti=not salvati)
        if salvati:
            with user_tx(owner, self._diario) as conn:
                self._usage(conn, owner, job_id, uso_col.model, "analisi", uso_col.prompt_tokens,
                            uso_col.output_tokens, uso_col.duration_ms)
                detail = f"collocazione rifatta, {uso_col.calls} richieste"
                if avvisi_col:
                    detail += f", {len(avvisi_col)} avvisi"
                self._transition(conn, owner, job_id, "dati_strutturati_salvati", detail)
            return
        self._salva_nota(owner, job_id, entry, out, temi, avvisi_col, uso_col)

    def _colloca(self, owner: UUID, job_id: UUID, entry_id: UUID, out: AnalysisOutcome, testo: str,
                 scrivi_appunti: bool):
        """Colloca ogni argomento nell'albero dei temi e, se è un'analisi nuova, salva gli appunti.

        Restituisce i temi dei blocchi, gli avvisi e l'uso del modello per la collocazione.
        """
        argomenti = [b.argomento for b in out.blocchi]
        testi_blocchi = [testo_blocco(b) for b in out.blocchi]
        with user_tx(owner, self._diario) as conn:
            senza = senza_embedding(conn)
        vecs, emb_model, emb_ms = self._embed(testi_blocchi + [label for _, label in senza])
        v_blocchi = vecs[:len(testi_blocchi)]
        with user_tx(owner, self._diario) as conn:
            salva_embedding_temi(conn, {tid: v for (tid, _), v in zip(senza, vecs[len(testi_blocchi):])})
            albero = albero_senza(conn, entry_id, v_blocchi)
            testo_albero, numerati = albero_testo(albero.radici, albero.vicini,
                                                  tutte_le_sintesi=len(albero.per_id) <= 15)
            date = indicazioni(conn)
            gia = confermati(conn, entry_id, argomenti)
            if vecs:
                self._usage(conn, owner, job_id, emb_model, "embedding", len(vecs), 0, emb_ms)
        proposte, uso = self.p.analyzer.colloca(out.blocchi, testo_albero, numerati, date)
        nomi = nomi_da_vettorizzare(proposte) + [x for xs in gia.values() for x in xs]
        nomi = list(dict.fromkeys(nomi))
        vn, emb_model, emb_ms = self._embed(nomi)
        vettori_nomi = dict(zip(nomi, vn))
        soglie = Soglie.da(self.s)
        testi_appunti = evo.testi_da_vettorizzare(out) if scrivi_appunti else []
        va, _, _ = self._embed(testi_appunti)

        with user_tx(owner, self._diario) as conn:
            decisioni = valuta(conn, argomenti, proposte, v_blocchi, vettori_nomi, albero, soglie, gia, date)
            if scrivi_appunti:
                # Ciò che una precedente analisi di questo vocale aveva fatto agli altri si rifà da capo.
                evo.annulla_evoluzione(conn, [entry_id])
            temi, avvisi = salva_collocazione(conn, owner, entry_id, argomenti, decisioni, vettori_nomi,
                                              soglie.unione)
            if scrivi_appunti:
                self._versione_appunti = conn.execute(
                    "SELECT coalesce(max(version), 0) + 1 AS v FROM entry_versions WHERE entry_id = %s",
                    (entry_id,)).fetchone()["v"]
                avvisi += evo.salva_appunti(conn, owner, entry_id, self._versione_appunti, out, temi, va, testo)
            conta_per_riordino(conn, owner)
            if vn or va:
                self._usage(conn, owner, job_id, emb_model, "embedding", len(vn) + len(va), 0, emb_ms)
        return temi, uso.warnings + avvisi, uso

    def _salva_nota(self, owner: UUID, job_id: UUID, entry: dict, out: AnalysisOutcome, temi: list,
                    avvisi: list[str], uso_col) -> None:
        data_ora = entry["data_ora"]
        if data_ora is not None:
            off = entry["data_ora_offset_minutes"]
            data_ora = data_ora.astimezone(timezone(timedelta(minutes=off)) if off is not None else timezone.utc)
        testo = entry["trascrizione_grezza"]
        with user_tx(owner, self._diario) as conn:
            version = self._versione_appunti
            etichette_temi = []
            for tid in temi:
                if tid:
                    r = conn.execute("SELECT label FROM topics WHERE id = %s", (tid,)).fetchone()
                    if r and r["label"] not in etichette_temi:
                        etichette_temi.append(r["label"])
            try:
                nota = Nota(
                    titolo=out.titolo, data_ora=data_ora, categoria=out.categoria,
                    tag=[t for t in etichette_temi if _TAG_OK(t)][:5],
                    executive_summary=out.sintesi(),
                    action_items=[ActionItem(descrizione=v.testo, scadenza=v.quando) for _, v in out.da_fare()],
                    trascrizione_grezza=testo,
                )
            except ValidationError as e:
                problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
                raise PipelineError(f"la nota non rispetta lo schema: {problems}", code="nota_non_valida") from e
            warnings = out.warnings + avvisi + soft_warnings(nota)
            conn.execute(
                """INSERT INTO entry_versions (owner_id, entry_id, version, note_json, origin, pipeline_version,
                                               model, prompt_version, warnings)
                   VALUES (%s, %s, %s, %s, 'analisi_automatica', %s, %s, %s, %s)""",
                (owner, entry["id"], version, Jsonb(nota.model_dump(mode="json")), PIPELINE_VERSION,
                 out.model, f"{PROMPT_VERSION}+{PROMPT_COLLOCA}", Jsonb(warnings)))
            conn.execute("UPDATE entries SET titolo = %s, categoria = %s, executive_summary = %s, "
                         "current_version = %s WHERE id = %s",
                         (nota.titolo, nota.categoria.value, nota.executive_summary, version, entry["id"]))
            self._usage(conn, owner, job_id, out.model, "analisi", out.prompt_tokens + uso_col.prompt_tokens,
                        out.output_tokens + uso_col.output_tokens, out.duration_ms + uso_col.duration_ms)
            calls = out.calls + uso_col.calls
            detail = f"{out.model}, {calls} richieste" + (f", {out.parti} parti" if out.parti > 1 else "")
            if warnings:
                detail += f", {len(warnings)} avvisi"
            self._transition(conn, owner, job_id, "dati_strutturati_salvati", f"versione {version}: {detail}")

    def _step_indicizzazione(self, owner: UUID, job_id: UUID) -> None:
        with user_tx(owner, self._diario) as conn:
            entry = conn.execute("SELECT e.id, e.trascrizione_grezza, j.payload AS job_payload FROM ingestion_jobs j "
                                 "JOIN entries e ON e.id = j.entry_id WHERE j.id = %s", (job_id,)).fetchone()
        if entry is None:
            raise PipelineError("nota non trovata per l'indicizzazione", code="nota_mancante")
        if (entry["job_payload"] or {}).get("modo") in ("colloca", "evolvi"):
            with user_tx(owner, self._diario) as conn:
                gia = conn.execute("SELECT char_start, char_end, embedding::text AS v FROM text_chunks "
                                   "WHERE entry_id = %s ORDER BY ord", (entry["id"],)).fetchall()
            if gia:
                passaggi = [(Span(r["char_start"], r["char_end"]), [float(x) for x in r["v"].strip("[]").split(",")])
                            for r in gia if r["v"]]
                esito = self._evolvi(owner, job_id, entry["id"], entry["trascrizione_grezza"], passaggi)
                with user_tx(owner, self._diario) as conn:
                    self._transition(conn, owner, job_id, "indicizzazione_completata",
                                     "segmenti già indicizzati" + (f"; {esito}" if esito else ""))
                return
        chunks = chunk_text(entry["trascrizione_grezza"])
        vectors, model, ms = self.p.embedder.embed([c.content for c in chunks])
        dims = {len(v) for v in vectors}
        if dims and dims != {self.s.embedding_dimensions}:
            raise PermanentError(
                f"il modello di embedding {model} produce vettori di {sorted(dims)} dimensioni, lo schema ne "
                f"prevede {self.s.embedding_dimensions}: usare bge-m3 o un modello a 1024 dimensioni",
                code="dimensione_embedding")
        with user_tx(owner, self._diario) as conn:
            conn.execute("DELETE FROM text_chunks WHERE entry_id = %s", (entry["id"],))
            for c, v in zip(chunks, vectors):
                conn.execute(
                    """INSERT INTO text_chunks (owner_id, entry_id, ord, char_start, char_end, content,
                                                embedding, embedding_model)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)""",
                    (owner, entry["id"], c.ord, c.char_start, c.char_end, c.content, _vector_literal(v), model))
            self._usage(conn, owner, job_id, model, "embedding", sum(len(c.content) for c in chunks),
                        len(chunks), ms)

        esito = self._evolvi(owner, job_id, entry["id"], entry["trascrizione_grezza"],
                             [(Span(c.char_start, c.char_end), v) for c, v in zip(chunks, vectors)])
        with user_tx(owner, self._diario) as conn:
            self._transition(conn, owner, job_id, "indicizzazione_completata",
                             f"{len(chunks)} segmenti indicizzati con {model}" + (f"; {esito}" if esito else ""))

    def _evolvi(self, owner: UUID, job_id: UUID, entry_id: UUID, testo: str,
                passaggi: list[tuple[Span, list[float]]]) -> str:
        """Ciò che il vocale cambia nel resto dell'archivio: cose fatte e schede dei temi."""
        an = self.p.analyzer
        if not hasattr(an, "aggiorna_tema"):
            return ""
        with user_tx(owner, self._diario) as conn:
            evo.annulla_evoluzione(conn, [entry_id])
            candidate = evo.candidate_chiusure(conn, entry_id, testo, passaggi)
        parti: list[str] = []

        if candidate:
            chiusure, uso = an.chiusure(testo, [c for _, c in candidate])
            with user_tx(owner, self._diario) as conn:
                n = sum(evo.chiudi(conn, owner, candidate[c.indice][0], c.esito, entry_id, c.fonte.text(testo))
                        for c in chiusure)
                self._usage(conn, owner, job_id, uso.model, "analisi", uso.prompt_tokens, uso.output_tokens,
                            uso.duration_ms)
            if n:
                parti.append(f"{n} {'cosa fatta' if n == 1 else 'cose fatte'}")

        with user_tx(owner, self._diario) as conn:
            temi = evo.temi_da_aggiornare(conn, entry_id)
        aggiornati = []
        for tid, dati, ids_punti in temi:
            r = an.aggiorna_tema(dati)
            vecs, emb_model, emb_ms = self._embed([evo.testo_profilo(dati.nome, r.sintesi)])
            with user_tx(owner, self._diario) as conn:
                evo.salva_tema(conn, tid, entry_id, r.sintesi, [ids_punti[i] for i in r.superati],
                               vecs[0] if vecs else None)
                self._usage(conn, owner, job_id, r.uso.model, "analisi", r.uso.prompt_tokens, r.uso.output_tokens,
                            r.uso.duration_ms)
            aggiornati.append(dati.nome)
        if aggiornati:
            parti.append("temi aggiornati: " + ", ".join(aggiornati))
        return "; ".join(parti)

    def _step_archiviazione(self, owner: UUID, job_id: UUID) -> None:
        with user_tx(owner, self._diario) as conn:
            self._transition(conn, owner, job_id, "archiviazione_completata", None)
            conn.execute("UPDATE ingestion_jobs SET completed_at = now(), locked_at = NULL, locked_by = NULL, "
                         "error_code = NULL, last_error = NULL, payload = payload - 'modo' WHERE id = %s", (job_id,))
        try:
            self.riordina_se_serve(owner)
        except ServiceUnavailable as e:
            log.warning("riordino rimandato: %s", e)
        except Exception:      # il riordino non deve mai far fallire una nota già archiviata
            log.exception("riordino dell'albero non riuscito")

    def riordina_se_serve(self, owner: UUID, forza: bool = False, diario: UUID | None = None) -> list[str]:
        """Riordina l'albero dei temi ogni tante note collocate (o quando la coda si svuota).

        Nel diario del job appena concluso, o in quello indicato (da riga di comando).
        """
        if diario is not None:
            self._diario = diario
        an = self.p.analyzer
        if not hasattr(an, "riordina"):
            return []
        with user_tx(owner, self._diario) as conn:
            if not forza and not da_riordinare(conn, self.s.riordino_ogni, self.s.riordino_ore):
                return []
            albero = carica_albero(conn)
            if len(albero.per_id) < 2:
                segna_riordino(conn, owner)
                return []
            testo, numerati = albero_testo(albero.radici, tutte_le_sintesi=True)
            date = indicazioni(conn)
        modifiche, uso = an.riordina(testo, numerati, date)
        with user_tx(owner, self._diario) as conn:
            fatto = applica_riordino(conn, owner, modifiche)
            segna_riordino(conn, owner)
            self._usage(conn, owner, None, uso.model, "analisi", uso.prompt_tokens, uso.output_tokens,
                        uso.duration_ms)
        for x in fatto:
            log.info("riordino: %s", x)
        for w in uso.warnings:
            log.info("riordino: %s", w)
        return fatto


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker(build_pipeline(s), s)

    def _stop(signum, _frame):
        log.info("arresto richiesto (segnale %s): finisco il passo in corso", signum)
        worker.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        worker.beat()
        worker.run_forever()
    finally:
        close_pool()


if __name__ == "__main__":
    main()
