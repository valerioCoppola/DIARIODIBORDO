-- Diario di Bordo — migrazione 0005: appunti riorganizzati e temi annidati.
--
-- L'analisi non produce più sintesi, cose da fare e appunti separati: riorganizza
-- il vocale in appunti ordinati. Ogni argomento del vocale è un blocco, collocato
-- in un tema; dentro, punti con i loro dettagli. Le cose da fare sono punti di tipo
-- "da_fare", legati alla cosa da fare che si spunta (action_items).
--
-- I temi formano un albero: un sotto-progetto o uno sviluppo interno è un tema
-- figlio del suo progetto (sulla mappa, una bolla dentro la bolla).

ALTER TABLE topics ADD COLUMN parent_id uuid;
ALTER TABLE topics ADD CONSTRAINT topics_parent_fk
  FOREIGN KEY (parent_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE SET NULL (parent_id);
ALTER TABLE topics ADD CONSTRAINT topics_parent_non_se_stesso CHECK (parent_id IS NULL OR parent_id <> id);
CREATE INDEX topics_parent_idx ON topics (parent_id);

ALTER TABLE punti DROP CONSTRAINT punti_tipo_check;
ALTER TABLE punti ADD CONSTRAINT punti_tipo_check
  CHECK (tipo IN ('punto', 'idea', 'decisione', 'domanda', 'fatto', 'da_fare'));
ALTER TABLE punti ADD COLUMN blocco smallint NOT NULL DEFAULT 0;   -- argomento del vocale, in ordine
ALTER TABLE punti ADD COLUMN parent_id uuid;                        -- dettaglio di un punto
ALTER TABLE punti ADD COLUMN action_item_id uuid;                   -- per i punti "da_fare"
ALTER TABLE punti ADD CONSTRAINT punti_parent_fk
  FOREIGN KEY (parent_id, owner_id) REFERENCES punti(id, owner_id) ON DELETE CASCADE;
ALTER TABLE punti ADD CONSTRAINT punti_azione_fk
  FOREIGN KEY (action_item_id, owner_id) REFERENCES action_items(id, owner_id) ON DELETE SET NULL (action_item_id);
CREATE INDEX punti_parent_idx ON punti (parent_id);
CREATE INDEX punti_azione_idx ON punti (action_item_id);
