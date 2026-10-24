-- Diario di Bordo — migrazione 0003: struttura della conoscenza e voci.
--
-- Aree: pochi ambiti stabili (Lavoro, Casa, Famiglia…) a cui appartiene ogni
-- argomento. Danno forma alla mappa: un'area per isola, gli argomenti dentro.
-- Gli argomenti ricordano il proprio embedding: un tema detto con parole
-- diverse ("macchina", "auto") confluisce in quello che esiste già.
--
-- Voci: chi parla in ogni segmento della trascrizione (riconoscimento locale
-- con pyannote) e i nomi dati a mano alle voci di ciascuna nota.

CREATE TABLE aree (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  slug       text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  nome       text NOT NULL,
  colore     smallint NOT NULL DEFAULT 0 CHECK (colore BETWEEN 0 AND 31),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  UNIQUE (owner_id, slug)
);

ALTER TABLE aree ENABLE ROW LEVEL SECURITY;
ALTER TABLE aree FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON aree
  USING (owner_id = app_current_user()) WITH CHECK (owner_id = app_current_user());
GRANT SELECT, INSERT, UPDATE, DELETE ON aree TO diario_app;

ALTER TABLE topics ADD COLUMN area_id uuid;
ALTER TABLE topics ADD COLUMN embedding vector(1024);
ALTER TABLE topics ADD CONSTRAINT topics_area_fk
  FOREIGN KEY (area_id, owner_id) REFERENCES aree(id, owner_id) ON DELETE SET NULL (area_id);
CREATE INDEX topics_area_idx ON topics (area_id);

-- Nomi dati alle voci di una nota: {"1": "Marco", "2": "Giulia"}.
ALTER TABLE entries ADD COLUMN voci_nomi jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Esito del riconoscimento delle voci sull'ultima trascrizione:
--   NULL o senza "stato": da fare (eventuale {"persone": N} come suggerimento)
--   {"stato": "ok", "persone": N, "dispositivo": "cuda", "secondi": 41}
--   {"stato": "non_preparato"} / {"stato": "errore", "messaggio": "..."}
-- Ogni segmento in transcriptions.segments può avere "voce": 1, 2, …
ALTER TABLE transcriptions ADD COLUMN voci jsonb;
