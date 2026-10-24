-- Diario di Bordo — migrazione 0004: la conoscenza che cresce.
--
-- Fino alla 0003 ogni vocale era una scheda a sé, con le sue etichette. Da qui
-- ogni argomento (tema) ha una scheda viva che si aggiorna a ogni vocale:
--
--   topics.sintesi     a che punto è il tema adesso, riscritta a ogni vocale che ne parla
--   topics.tipo        progetto | idea | tema
--   topics.profilo     embedding di nome + sintesi: per ritrovare il tema giusto
--                      quando arriva un vocale che ne parla con altre parole
--   punti              ciò che resta di ogni vocale: idee, decisioni, domande, fatti,
--                      ciascuno con la frase che lo giustifica. Un vocale successivo
--                      può renderne superato uno (decisione cambiata, domanda risolta).
--
-- Cose da fare: appartengono a un tema; un impegno ripetuto in un altro vocale
-- non si duplica (evento "ribadita"); un vocale che dice di averlo fatto lo chiude
-- (evento "completata" di origine automatica, con il vocale che lo dice).

ALTER TABLE topics ADD COLUMN tipo text CHECK (tipo IN ('progetto', 'idea', 'tema'));
ALTER TABLE topics ADD COLUMN sintesi text;
ALTER TABLE topics ADD COLUMN sintesi_il timestamptz;
ALTER TABLE topics ADD COLUMN profilo vector(1024);

CREATE TABLE punti (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  entry_id    uuid NOT NULL,                  -- il vocale da cui viene
  topic_id    uuid,                           -- il tema a cui appartiene
  tipo        text NOT NULL CHECK (tipo IN ('idea', 'decisione', 'domanda', 'fatto')),
  testo       text NOT NULL,
  char_start  integer,
  char_end    integer,
  quote       text,
  ord         integer,
  stato       text NOT NULL DEFAULT 'attivo' CHECK (stato IN ('attivo', 'superato')),
  superato_da uuid,                           -- il vocale che lo ha reso superato
  embedding   vector(1024),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (topic_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (superato_da, owner_id) REFERENCES entries(id, owner_id) ON DELETE SET NULL (superato_da)
);
CREATE INDEX punti_topic_idx ON punti (topic_id, stato);
CREATE INDEX punti_entry_idx ON punti (entry_id);

ALTER TABLE punti ENABLE ROW LEVEL SECURITY;
ALTER TABLE punti FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON punti
  USING (owner_id = app_current_user()) WITH CHECK (owner_id = app_current_user());
GRANT SELECT, INSERT, UPDATE, DELETE ON punti TO diario_app;

ALTER TABLE action_items ADD COLUMN topic_id uuid;
ALTER TABLE action_items ADD COLUMN embedding vector(1024);
ALTER TABLE action_items ADD CONSTRAINT action_items_topic_fk
  FOREIGN KEY (topic_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE SET NULL (topic_id);
CREATE INDEX action_items_topic_idx ON action_items (topic_id, stato);

ALTER TABLE action_item_events DROP CONSTRAINT action_item_events_event_check;
ALTER TABLE action_item_events ADD CONSTRAINT action_item_events_event_check
  CHECK (event IN ('completata', 'riaperta', 'annullata', 'modificata', 'ribadita'));
CREATE INDEX action_item_events_source_idx ON action_item_events (source_entry_id);
