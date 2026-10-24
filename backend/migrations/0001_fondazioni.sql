-- Diario di Bordo — migrazione 0001: fondazioni
-- Crea estensioni, ruolo applicativo, tutte le entità del modello dati (spec. §5)
-- e le politiche Row Level Security. Eseguita dal runner come proprietario del DB.

CREATE EXTENSION IF NOT EXISTS vector;

-- Ruolo usato dal backend a runtime: non proprietario delle tabelle, senza BYPASSRLS.
-- La password viene impostata dal runner (variabile DB_APP_PASSWORD), non qui.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'diario_app') THEN
    CREATE ROLE diario_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

-- Utente corrente della richiesta, impostato dal backend con set_config(..., true)
-- all'inizio di ogni transazione. Nessun valore => nessuna riga visibile.
CREATE FUNCTION app_current_user() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- Tipi enumerati
-- ---------------------------------------------------------------------------

CREATE TYPE categoria_nota AS ENUM
  ('Lavoro', 'Idea', 'Riassunto giornata', 'Riflessione', 'Personale', 'Altro');

-- Provenienza di una registrazione o di una nota.
CREATE TYPE provenienza AS ENUM
  ('registratore', 'caricamento', 'chatgpt', 'api', 'altro');

-- Stati della pipeline (spec. §9) più gli stati di errore.
CREATE TYPE stato_job AS ENUM (
  'ricevuto',
  'originale_conservato',
  'trascrizione_in_corso',
  'trascrizione_completata',
  'analisi_in_corso',
  'dati_strutturati_salvati',
  'indicizzazione_completata',
  'archiviazione_completata',
  'errore',            -- ritentabile
  'errore_definitivo', -- tentativi esauriti o errore non recuperabile
  'annullato'
);

-- Origine di un'informazione: dichiarata esplicitamente nella fonte,
-- estratta automaticamente da un modello, o inserita/corretta a mano.
CREATE TYPE origine_info AS ENUM ('dichiarato', 'estratto_automatico', 'manuale');

-- ---------------------------------------------------------------------------
-- Autenticazione (senza RLS: servono prima di conoscere l'utente)
-- ---------------------------------------------------------------------------

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username      text NOT NULL CHECK (username ~ '^[a-z0-9._-]{3,64}$'),
  password_hash text NOT NULL,
  disabled      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);
CREATE UNIQUE INDEX users_username_key ON users (username);

CREATE TABLE sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   bytea NOT NULL UNIQUE,      -- sha256 del token, il token non è salvato
  csrf_token   text NOT NULL,
  user_agent   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  revoked_at   timestamptz
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

-- Token per script e scorciatoie (es. importazione da ChatGPT, Comandi Rapidi iOS).
CREATE TABLE api_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         text NOT NULL,
  token_hash   bytea NOT NULL UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at   timestamptz
);

-- ---------------------------------------------------------------------------
-- recordings — l'audio originale, mai modificato
-- ---------------------------------------------------------------------------

CREATE TABLE recordings (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source             provenienza NOT NULL,
  storage_path       text NOT NULL,           -- relativo alla radice dello storage
  original_filename  text,
  mime_type          text NOT NULL,
  format             text,                    -- es. webm/opus, m4a/aac
  size_bytes         bigint NOT NULL CHECK (size_bytes > 0),
  duration_seconds   numeric(10,3) CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
  sha256             bytea NOT NULL CHECK (octet_length(sha256) = 32),
  -- Inizio effettivo della registrazione, solo se da metadati affidabili.
  recorded_at        timestamptz,
  recorded_at_source text CHECK (recorded_at_source IN ('registratore_integrato', 'metadati_file', 'dichiarato_utente')),
  -- timestamptz non conserva l'offset: lo salviamo a parte per riprodurre l'ISO 8601 originale.
  recorded_utc_offset_minutes smallint CHECK (recorded_utc_offset_minutes BETWEEN -840 AND 840),
  client_metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
  storage_status     text NOT NULL DEFAULT 'conservato'
                     CHECK (storage_status IN ('conservato', 'verificato', 'mancante', 'corrotto', 'eliminato')),
  last_verified_at   timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  UNIQUE (owner_id, sha256)                   -- deduplicazione per contenuto
);
CREATE INDEX recordings_owner_created_idx ON recordings (owner_id, created_at DESC);
CREATE TRIGGER recordings_updated_at BEFORE UPDATE ON recordings
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Sessioni di caricamento a blocchi, riprendibili dopo una perdita di connessione.
CREATE TABLE upload_sessions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  idempotency_key  text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
  source           provenienza NOT NULL,
  filename         text,
  mime_type        text NOT NULL,
  total_size       bigint NOT NULL CHECK (total_size > 0),
  chunk_size       integer NOT NULL CHECK (chunk_size > 0),
  total_chunks     integer NOT NULL CHECK (total_chunks > 0),
  received_chunks  integer[] NOT NULL DEFAULT '{}',
  expected_sha256  bytea CHECK (expected_sha256 IS NULL OR octet_length(expected_sha256) = 32),
  recorded_at      timestamptz,
  recorded_at_source text,
  recorded_utc_offset_minutes smallint,
  duration_seconds numeric(10,3),
  client_metadata  jsonb NOT NULL DEFAULT '{}'::jsonb,
  status           text NOT NULL DEFAULT 'aperta'
                   CHECK (status IN ('aperta', 'completata', 'annullata', 'scaduta')),
  recording_id     uuid,
  job_id           uuid,
  was_duplicate    boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (recording_id, owner_id) REFERENCES recordings(id, owner_id)
);
CREATE TRIGGER upload_sessions_updated_at BEFORE UPDATE ON upload_sessions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- entries — la nota; entry_versions — ogni analisi o correzione
-- ---------------------------------------------------------------------------

CREATE TABLE entries (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recording_id        uuid,
  titolo              text,
  data_ora            timestamptz,
  data_ora_offset_minutes smallint CHECK (data_ora_offset_minutes BETWEEN -840 AND 840),
  categoria           categoria_nota NOT NULL DEFAULT 'Altro',
  executive_summary   text[] CHECK (executive_summary IS NULL OR cardinality(executive_summary) = 3),
  trascrizione_grezza text,                   -- testo integrale, mai riscritto
  source              provenienza NOT NULL,
  source_reference    text,                   -- es. titolo conversazione ChatGPT
  processing_status   stato_job NOT NULL DEFAULT 'ricevuto',
  current_version     integer,
  transcript_sha256   bytea,                  -- per riconciliare duplicati testuali
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  search_tsv          tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('italian', coalesce(titolo, '')), 'A') ||
                        setweight(to_tsvector('italian', coalesce(trascrizione_grezza, '')), 'C')
                      ) STORED,
  UNIQUE (id, owner_id),
  FOREIGN KEY (recording_id, owner_id) REFERENCES recordings(id, owner_id)
);
CREATE INDEX entries_owner_date_idx ON entries (owner_id, (coalesce(data_ora, created_at)) DESC);
CREATE INDEX entries_search_idx ON entries USING gin (search_tsv);
CREATE UNIQUE INDEX entries_recording_key ON entries (recording_id) WHERE recording_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX entries_transcript_sha_idx ON entries (owner_id, transcript_sha256);
CREATE TRIGGER entries_updated_at BEFORE UPDATE ON entries
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE entry_versions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id         uuid NOT NULL,
  entry_id         uuid NOT NULL,
  version          integer NOT NULL CHECK (version > 0),
  note_json        jsonb NOT NULL,            -- JSON canonico (spec. §3)
  schema_version   text NOT NULL DEFAULT '1',
  origin           text NOT NULL CHECK (origin IN ('analisi_automatica', 'importazione', 'correzione_manuale')),
  pipeline_version text,                      -- versione del processo di analisi
  model            text,                      -- es. modello di trascrizione/analisi
  model_version    text,
  prompt_version   text,
  correction_note  text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entry_id, version),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- topics / entry_topics — vocabolario normalizzato degli argomenti
-- ---------------------------------------------------------------------------

CREATE TABLE topics (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  slug        text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  label       text NOT NULL,
  aliases     text[] NOT NULL DEFAULT '{}',
  merged_into uuid,                           -- normalizzazione: etichetta fusa in un'altra
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  UNIQUE (owner_id, slug),
  FOREIGN KEY (merged_into, owner_id) REFERENCES topics(id, owner_id)
);

CREATE TABLE entry_topics (
  owner_id   uuid NOT NULL,
  entry_id   uuid NOT NULL,
  topic_id   uuid NOT NULL,
  origin     origine_info NOT NULL,           -- tag dichiarato vs argomento estratto
  confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entry_id, topic_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (topic_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX entry_topics_topic_idx ON entry_topics (topic_id);

-- Frequenza e ricorrenza degli argomenti.
CREATE VIEW topic_stats WITH (security_invoker = true) AS
SELECT t.id AS topic_id, t.owner_id, t.slug, t.label,
       count(et.entry_id)                         AS entry_count,
       count(DISTINCT date_trunc('day', coalesce(e.data_ora, e.created_at))) AS distinct_days,
       min(coalesce(e.data_ora, e.created_at))    AS first_seen,
       max(coalesce(e.data_ora, e.created_at))    AS last_seen
FROM topics t
LEFT JOIN entry_topics et ON et.topic_id = t.id
LEFT JOIN entries e ON e.id = et.entry_id AND e.deleted_at IS NULL
WHERE t.merged_into IS NULL
GROUP BY t.id;

-- ---------------------------------------------------------------------------
-- text_chunks — segmenti indicizzati (testuale + vettoriale)
-- ---------------------------------------------------------------------------

-- Dimensione 1024: modelli di embedding multilingue locali (es. bge-m3 via Ollama).
-- Cambiare modello con dimensione diversa richiede una migrazione e un ricalcolo.
CREATE TABLE text_chunks (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL,
  entry_id        uuid NOT NULL,
  ord             integer NOT NULL CHECK (ord >= 0),
  char_start      integer NOT NULL CHECK (char_start >= 0),
  char_end        integer NOT NULL,
  content         text NOT NULL,
  token_count     integer,
  embedding       vector(1024),
  embedding_model text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  search_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('italian', content)) STORED,
  CHECK (char_end > char_start),
  UNIQUE (id, owner_id),
  UNIQUE (entry_id, ord),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX text_chunks_embedding_idx ON text_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX text_chunks_search_idx ON text_chunks USING gin (search_tsv);

-- ---------------------------------------------------------------------------
-- entities — persone, organizzazioni, luoghi, progetti
-- ---------------------------------------------------------------------------

CREATE TABLE entities (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('persona', 'organizzazione', 'luogo', 'progetto', 'altro')),
  name            text NOT NULL,
  normalized_name text NOT NULL,
  aliases         text[] NOT NULL DEFAULT '{}',
  description     text,
  merged_into     uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  UNIQUE (owner_id, kind, normalized_name),
  FOREIGN KEY (merged_into, owner_id) REFERENCES entities(id, owner_id)
);
CREATE TRIGGER entities_updated_at BEFORE UPDATE ON entities
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Ogni menzione punta al passaggio originale che la giustifica.
CREATE TABLE entity_mentions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id   uuid NOT NULL,
  entity_id  uuid NOT NULL,
  entry_id   uuid NOT NULL,
  char_start integer,
  char_end   integer,
  quote      text,
  origin     origine_info NOT NULL,
  confidence real,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (entity_id, owner_id) REFERENCES entities(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX entity_mentions_entity_idx ON entity_mentions (entity_id);
CREATE INDEX entity_mentions_entry_idx ON entity_mentions (entry_id);

CREATE TABLE entity_relations (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id          uuid NOT NULL,
  subject_entity_id uuid NOT NULL,
  predicate         text NOT NULL,            -- es. "lavora_per", "partecipa_a"
  object_entity_id  uuid NOT NULL,
  entry_id          uuid NOT NULL,            -- fonte documentata della relazione
  char_start        integer,
  char_end          integer,
  quote             text,
  origin            origine_info NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (subject_entity_id, owner_id) REFERENCES entities(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (object_entity_id, owner_id) REFERENCES entities(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- decisions
-- ---------------------------------------------------------------------------

CREATE TABLE decisions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL,
  entry_id      uuid NOT NULL,                -- fonte
  entry_version integer,
  descrizione   text NOT NULL,
  contesto      text,
  decided_at    timestamptz,                  -- solo quando determinabile
  date_source   text CHECK (date_source IN ('data_registrazione', 'esplicita_nel_testo', 'relativa_risolta')),
  char_start    integer,
  char_end      integer,
  quote         text,
  origin        origine_info NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX decisions_owner_date_idx ON decisions (owner_id, decided_at DESC NULLS LAST);

-- ---------------------------------------------------------------------------
-- action_items
-- ---------------------------------------------------------------------------

CREATE TABLE action_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,
  entry_id       uuid NOT NULL,               -- fonte
  entry_version  integer,
  descrizione    text NOT NULL,
  responsabile   text,                        -- null se non indicato
  scadenza       text,                        -- come nel JSON canonico
  scadenza_data  date,                        -- valorizzata solo se risolvibile con certezza
  stato          text NOT NULL DEFAULT 'aperta' CHECK (stato IN ('aperta', 'completata', 'annullata')),
  char_start     integer,
  char_end       integer,
  quote          text,
  origin         origine_info NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX action_items_owner_stato_idx ON action_items (owner_id, stato, scadenza_data);
CREATE TRIGGER action_items_updated_at BEFORE UPDATE ON action_items
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Storico delle conferme di completamento, anche da note successive.
CREATE TABLE action_item_events (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL,
  action_item_id  uuid NOT NULL,
  event           text NOT NULL CHECK (event IN ('completata', 'riaperta', 'annullata', 'modificata')),
  source_entry_id uuid,
  note            text,
  origin          origine_info NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (action_item_id, owner_id) REFERENCES action_items(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (source_entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE SET NULL (source_entry_id)
);

-- ---------------------------------------------------------------------------
-- ingestion_jobs — coda persistente della pipeline
-- ---------------------------------------------------------------------------

CREATE TABLE ingestion_jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('audio', 'import_chatgpt', 'rianalisi', 'reindicizzazione')),
  idempotency_key text NOT NULL,
  recording_id    uuid,
  entry_id        uuid,
  status          stato_job NOT NULL DEFAULT 'ricevuto',
  failed_stage    stato_job,                  -- stato in cui è avvenuto l'ultimo errore
  attempts        integer NOT NULL DEFAULT 0,
  max_attempts    integer NOT NULL DEFAULT 5,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  locked_at       timestamptz,
  locked_by       text,
  error_code      text,
  last_error      text,                       -- solo messaggi tecnici, nessun contenuto
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  UNIQUE (id, owner_id),
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (recording_id, owner_id) REFERENCES recordings(id, owner_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id)
);
-- Prelievo dalla coda: SELECT ... FOR UPDATE SKIP LOCKED su questi stati.
CREATE INDEX ingestion_jobs_queue_idx ON ingestion_jobs (next_attempt_at)
  WHERE status NOT IN ('archiviazione_completata', 'errore_definitivo', 'annullato');
CREATE TRIGGER ingestion_jobs_updated_at BEFORE UPDATE ON ingestion_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE job_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id    uuid NOT NULL,
  job_id      uuid NOT NULL,
  from_status stato_job,
  to_status   stato_job NOT NULL,
  message     text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (job_id, owner_id) REFERENCES ingestion_jobs(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX job_events_job_idx ON job_events (job_id, id);

-- Consumi dei modelli (anche locali: durata, unità elaborate) per il Monitoraggio.
CREATE TABLE model_usage (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id        uuid,
  provider      text NOT NULL,                -- 'locale', 'openai', ...
  model         text NOT NULL,
  operation     text NOT NULL CHECK (operation IN ('trascrizione', 'analisi', 'embedding', 'risposta')),
  input_units   bigint,                       -- secondi di audio o token in ingresso
  output_units  bigint,
  duration_ms   integer,
  cost_estimate numeric(12,6) NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_usage_owner_created_idx ON model_usage (owner_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Row Level Security: ogni riga è visibile solo al proprietario
-- ---------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'recordings', 'upload_sessions', 'entries', 'entry_versions', 'topics', 'entry_topics',
    'text_chunks', 'entities', 'entity_mentions', 'entity_relations', 'decisions',
    'action_items', 'action_item_events', 'ingestion_jobs', 'job_events', 'model_usage'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON %I USING (owner_id = app_current_user()) '
      'WITH CHECK (owner_id = app_current_user())', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO diario_app', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON users, sessions, api_tokens TO diario_app;
GRANT SELECT ON topic_stats TO diario_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO diario_app;
GRANT EXECUTE ON FUNCTION app_current_user() TO diario_app;
