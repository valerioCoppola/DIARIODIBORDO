-- Diario di Bordo — migrazione 0007: più diari per persona, più persone
--
-- Diari. Ogni persona ha uno o più diari, ciascuno con il suo archivio, la sua mappa
-- dei temi, le sue domande e le sue cose da fare: nulla passa da un diario all'altro.
-- L'isolamento è nel database, come quello tra utenti: ogni tabella di contenuto ha
-- diario_id e una seconda politica RLS (restrittiva, quindi in AND con quella del
-- proprietario) che mostra solo le righe del diario impostato per la transazione
-- (app.diario_id). Nessun diario impostato => nessuna riga visibile. Le righe nuove
-- prendono il diario della transazione da sole (DEFAULT), quindi il codice che le
-- scrive non deve passarlo.
--
-- Il diario aperto è salvato sulla sessione (ogni telefono o browser ha la sua):
-- le richieste di quella sessione lavorano in quel diario. Un caricamento dal
-- telefono può indicare il suo diario con l'header X-Diario (il vocale registrato
-- in un diario arriva lì anche se nel frattempo se ne è aperto un altro).
--
-- Tutto ciò che c'era già finisce nel primo diario di ogni persona, "Diario di Bordo".
--
-- Persone. Chi c'era già diventa amministratore (l'installazione è sua). Gli altri
-- account nascono da un invito che l'amministratore crea: codice monouso con scadenza,
-- salvato solo come sha256. L'amministratore gestisce gli account (attiva, disattiva,
-- password provvisoria) ma non vede i diari degli altri: le politiche RLS valgono anche
-- per lui.

-- ---------------------------------------------------------------------------
-- Persone
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN is_admin boolean NOT NULL DEFAULT false;
-- Password provvisoria data dall'amministratore: al prossimo accesso va cambiata.
ALTER TABLE users ADD COLUMN cambia_password boolean NOT NULL DEFAULT false;
UPDATE users SET is_admin = true;

CREATE TABLE inviti (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  codice_hash  bytea NOT NULL UNIQUE,          -- sha256 del codice, il codice non è salvato
  nota         text CHECK (nota IS NULL OR length(nota) <= 80),   -- es. "per Marta"
  creato_da    uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  scade_il     timestamptz NOT NULL,
  usato_da     uuid REFERENCES users(id) ON DELETE SET NULL,
  usato_il     timestamptz,
  revocato_il  timestamptz
);
GRANT SELECT, INSERT, UPDATE, DELETE ON inviti TO diario_app;

-- ---------------------------------------------------------------------------
-- Diari
-- ---------------------------------------------------------------------------

CREATE FUNCTION app_current_diario() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.diario_id', true), '')::uuid
$$;
GRANT EXECUTE ON FUNCTION app_current_diario() TO diario_app;

CREATE TABLE diari (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  nome        text NOT NULL CHECK (length(btrim(nome)) BETWEEN 1 AND 40),
  copertina   text NOT NULL DEFAULT 'cuoio'
              CHECK (copertina IN ('cuoio', 'notte', 'bosco', 'vino', 'ardesia', 'senape')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  aperto_il   timestamptz,                    -- ultima apertura: in testa sullo scaffale
  UNIQUE (id, owner_id)
);
CREATE INDEX diari_owner_idx ON diari (owner_id);
ALTER TABLE diari ENABLE ROW LEVEL SECURITY;
ALTER TABLE diari FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON diari USING (owner_id = app_current_user())
  WITH CHECK (owner_id = app_current_user());
GRANT SELECT, INSERT, UPDATE, DELETE ON diari TO diario_app;

ALTER TABLE sessions ADD COLUMN diario_id uuid REFERENCES diari(id) ON DELETE SET NULL;

INSERT INTO diari (owner_id, nome, aperto_il) SELECT id, 'Diario di Bordo', now() FROM users;
-- Le sessioni aperte restano dentro il diario: dopo l'aggiornamento si continua da dove si era.
UPDATE sessions s SET diario_id = d.id FROM diari d WHERE d.owner_id = s.user_id;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'recordings', 'upload_sessions', 'entries', 'entry_versions', 'topics', 'entry_topics',
    'text_chunks', 'entities', 'entity_mentions', 'entity_relations', 'decisions',
    'action_items', 'action_item_events', 'ingestion_jobs', 'job_events', 'model_usage',
    'transcriptions', 'punti', 'blocchi', 'chiarimenti', 'indicazioni', 'stato_albero'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN diario_id uuid', t);
    -- Senza i trigger: updated_at e la sveglia del worker non devono accorgersene.
    EXECUTE format('ALTER TABLE %I DISABLE TRIGGER USER', t);
    EXECUTE format('UPDATE %I x SET diario_id = d.id FROM diari d WHERE d.owner_id = x.owner_id', t);
    EXECUTE format('ALTER TABLE %I ENABLE TRIGGER USER', t);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN diario_id SET DEFAULT app_current_diario()', t);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN diario_id SET NOT NULL', t);
    -- Eliminare un diario elimina tutto ciò che contiene (i file audio li toglie il servizio).
    EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (diario_id, owner_id) '
                   'REFERENCES diari(id, owner_id) ON DELETE CASCADE', t, t || '_diario_fk');
    EXECUTE format('CREATE INDEX %I ON %I (diario_id)', t || '_diario_idx', t);
    EXECUTE format(
      'CREATE POLICY diario_isolation ON %I AS RESTRICTIVE USING (diario_id = app_current_diario()) '
      'WITH CHECK (diario_id = app_current_diario())', t);
  END LOOP;
END $$;

-- Unicità che valgono dentro un diario, non più per tutta la persona: lo stesso audio
-- può stare in due diari, lo stesso nome di tema pure.
ALTER TABLE recordings DROP CONSTRAINT recordings_owner_id_sha256_key;
ALTER TABLE recordings ADD CONSTRAINT recordings_diario_sha256_key UNIQUE (owner_id, diario_id, sha256);
ALTER TABLE upload_sessions DROP CONSTRAINT upload_sessions_owner_id_idempotency_key_key;
ALTER TABLE upload_sessions ADD CONSTRAINT upload_sessions_diario_key UNIQUE (owner_id, diario_id, idempotency_key);
ALTER TABLE ingestion_jobs DROP CONSTRAINT ingestion_jobs_owner_id_idempotency_key_key;
ALTER TABLE ingestion_jobs ADD CONSTRAINT ingestion_jobs_diario_key UNIQUE (owner_id, diario_id, idempotency_key);
ALTER TABLE topics DROP CONSTRAINT topics_owner_id_slug_key;
ALTER TABLE topics ADD CONSTRAINT topics_diario_slug_key UNIQUE (owner_id, diario_id, slug);
ALTER TABLE entities DROP CONSTRAINT entities_owner_id_kind_normalized_name_key;
ALTER TABLE entities ADD CONSTRAINT entities_diario_name_key UNIQUE (owner_id, diario_id, kind, normalized_name);
ALTER TABLE stato_albero DROP CONSTRAINT stato_albero_pkey;
ALTER TABLE stato_albero ADD PRIMARY KEY (owner_id, diario_id);

-- ---------------------------------------------------------------------------
-- Coda: il worker deve sapere anche in quale diario lavorare
-- ---------------------------------------------------------------------------

DROP FUNCTION claim_next_job(text, integer);
CREATE FUNCTION claim_next_job(p_worker text, p_lease_seconds integer)
RETURNS TABLE (o_job_id uuid, o_owner_id uuid, o_diario_id uuid)
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
  RETURNING j.id, j.owner_id, j.diario_id;
END $$;

REVOKE ALL ON FUNCTION claim_next_job(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_next_job(text, integer) TO diario_app;
