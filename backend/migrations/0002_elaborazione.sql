-- Diario di Bordo — migrazione 0002: elaborazione (Incremento 2)
-- Trascrizioni con i tempi dei segmenti, avvisi delle analisi, prelievo dei job
-- dalla coda per il worker, notifica dei nuovi job, battito del worker.

-- ---------------------------------------------------------------------------
-- transcriptions — risultato della trascrizione, con i segmenti temporizzati
-- ---------------------------------------------------------------------------

CREATE TABLE transcriptions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id             uuid NOT NULL,
  recording_id         uuid NOT NULL,
  entry_id             uuid NOT NULL,
  model                text NOT NULL,             -- es. faster-whisper large-v3
  device               text NOT NULL,             -- cuda / cpu
  language             text,
  language_probability real,
  audio_seconds        numeric(10,3),
  speech_seconds       numeric(10,3),             -- dopo il filtro dei silenzi
  -- [{inizio, fine, char_start, char_end}]: posizione di ogni segmento nel testo
  -- di entries.trascrizione_grezza, per ascoltare il punto esatto dell'audio.
  segments             jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at           timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (recording_id, owner_id) REFERENCES recordings(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX transcriptions_entry_idx ON transcriptions (entry_id, created_at DESC);

ALTER TABLE transcriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcriptions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON transcriptions
  USING (owner_id = app_current_user()) WITH CHECK (owner_id = app_current_user());
GRANT SELECT, INSERT, UPDATE, DELETE ON transcriptions TO diario_app;

-- Avvisi della validazione (es. titolo fuori misura, responsabile non presente
-- nel testo e quindi rimosso): restano accanto alla versione che li ha prodotti.
ALTER TABLE entry_versions ADD COLUMN warnings jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Un'azione estratta automaticamente si sostituisce a ogni nuova analisi;
-- una modificata a mano (stato cambiato) no.
ALTER TABLE action_items ADD COLUMN ord integer;

-- ---------------------------------------------------------------------------
-- Coda: prelievo di un job da parte del worker
-- ---------------------------------------------------------------------------
-- Il worker usa il ruolo applicativo, soggetto a RLS, e non conosce in anticipo
-- il proprietario del prossimo job. Questa funzione, eseguita con i diritti del
-- proprietario dello schema, restituisce SOLO id e proprietario del job preso:
-- tutto il resto del lavoro avviene poi in transazioni con app.user_id impostato.
--
-- Prelievo con FOR UPDATE SKIP LOCKED: due worker non prendono mai lo stesso job.
-- Il job resta "affittato" (locked_at) e il worker rinnova l'affitto con un
-- battito; se il worker muore, dopo p_lease_seconds il job torna prelevabile.

CREATE FUNCTION claim_next_job(p_worker text, p_lease_seconds integer)
RETURNS TABLE (o_job_id uuid, o_owner_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  RETURN QUERY
  UPDATE ingestion_jobs j
     SET locked_at = now(), locked_by = p_worker, attempts = j.attempts + 1
   WHERE j.id = (
         SELECT q.id FROM ingestion_jobs q
          WHERE q.status NOT IN ('archiviazione_completata', 'errore_definitivo', 'annullato')
            AND q.next_attempt_at <= now()
            AND (q.locked_at IS NULL OR q.locked_at < now() - make_interval(secs => p_lease_seconds))
          ORDER BY q.next_attempt_at, q.created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
  RETURNING j.id, j.owner_id;
END $$;

REVOKE ALL ON FUNCTION claim_next_job(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_next_job(text, integer) TO diario_app;

-- Nuovo job o job rimesso in coda: sveglia il worker (LISTEN diario_jobs).
CREATE FUNCTION notify_job() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('diario_jobs', NEW.id::text);
  RETURN NEW;
END $$;

CREATE TRIGGER ingestion_jobs_notify_insert AFTER INSERT ON ingestion_jobs
  FOR EACH ROW EXECUTE FUNCTION notify_job();
CREATE TRIGGER ingestion_jobs_notify_requeue AFTER UPDATE OF next_attempt_at ON ingestion_jobs
  FOR EACH ROW WHEN (NEW.locked_at IS NULL AND NEW.next_attempt_at <= now())
  EXECUTE FUNCTION notify_job();

-- ---------------------------------------------------------------------------
-- Battito del worker, per mostrare nell'interfaccia se l'elaborazione è attiva
-- ---------------------------------------------------------------------------
-- Nessun contenuto: solo identità del processo, dispositivo e modelli in uso.

CREATE TABLE worker_heartbeats (
  worker_id    text PRIMARY KEY,
  started_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  info         jsonb NOT NULL DEFAULT '{}'::jsonb
);
GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO diario_app;
