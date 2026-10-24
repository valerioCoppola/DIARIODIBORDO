-- Diario di Bordo — migrazione 0006: un albero che nasce da ciò che si dice.
--
-- Fino alla 0005 ogni tema stava in un'"area" scelta tra poche, con un colore fisso:
-- un livello deciso in anticipo, che finiva per contenere categorie ("Progetti") invece
-- delle cose. Da qui c'è solo l'albero dei temi: in cima stanno i temi più ampi emersi
-- finora (sulla mappa, le isole) e un livello più ampio nasce quando più temi si
-- rivelano parti di una stessa cosa. I colori si calcolano dalla forma dell'albero.
--
--   blocchi      gli argomenti di ogni vocale, con il tema in cui sono collocati, quanto
--                era sicura la collocazione e, se la persona l'ha confermata o corretta,
--                il percorso giusto (resta anche quando l'albero si ricostruisce da capo)
--   chiarimenti  le domande che il sistema fa quando non è sicuro di dove va qualcosa
--                o se un nome è quello di un tema che esiste già
--   indicazioni  ciò che la persona ha detto rispondendo o correggendo: il modello le
--                rilegge ogni volta che colloca, così la stessa domanda non torna
--   stato_albero quando l'albero è stato riordinato l'ultima volta

ALTER TABLE topics DROP CONSTRAINT topics_area_fk;
DROP INDEX IF EXISTS topics_area_idx;
ALTER TABLE topics DROP COLUMN area_id;
DROP TABLE aree;

-- provvisorio: nato da una collocazione incerta, finché qualcosa non lo conferma.
-- fissato: messo al suo posto (o rinominato) dalla persona; il riordino non lo tocca.
ALTER TABLE topics ADD COLUMN provvisorio boolean NOT NULL DEFAULT false;
ALTER TABLE topics ADD COLUMN fissato boolean NOT NULL DEFAULT false;

-- Un tema che sparisce non porta via gli appunti che conteneva.
ALTER TABLE punti DROP CONSTRAINT punti_topic_id_owner_id_fkey;
ALTER TABLE punti ADD CONSTRAINT punti_topic_fk
  FOREIGN KEY (topic_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE SET NULL (topic_id);

CREATE TABLE blocchi (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  entry_id    uuid NOT NULL,
  ord         smallint NOT NULL,              -- = punti.blocco
  argomento   text NOT NULL,
  topic_id    uuid,
  certezza    text NOT NULL DEFAULT 'alta' CHECK (certezza IN ('alta', 'media', 'bassa')),
  confermato  jsonb,                          -- ["tema ampio", "parte", "cosa"]: deciso dalla persona
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  UNIQUE (entry_id, ord),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (topic_id, owner_id) REFERENCES topics(id, owner_id) ON DELETE SET NULL (topic_id)
);
CREATE INDEX blocchi_topic_idx ON blocchi (topic_id);

CREATE TABLE chiarimenti (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  entry_id    uuid NOT NULL,
  blocco      smallint NOT NULL,
  tipo        text NOT NULL CHECK (tipo IN ('collocazione', 'nome')),
  domanda     text NOT NULL,
  -- [{"etichetta": "A › B", "percorso": [{"id": "…" | null, "nome": "A"}, …]}, …]
  opzioni     jsonb NOT NULL,
  stato       text NOT NULL DEFAULT 'aperta' CHECK (stato IN ('aperta', 'risposta', 'superata')),
  risposta    jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  risposto_il timestamptz,
  UNIQUE (id, owner_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX chiarimenti_aperti_idx ON chiarimenti (owner_id, stato, created_at);
CREATE INDEX chiarimenti_entry_idx ON chiarimenti (entry_id);

CREATE TABLE indicazioni (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  testo       text NOT NULL CHECK (length(testo) BETWEEN 1 AND 600),
  entry_id    uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  FOREIGN KEY (entry_id, owner_id) REFERENCES entries(id, owner_id) ON DELETE SET NULL (entry_id)
);

CREATE TABLE stato_albero (
  owner_id          uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  note_dal_riordino integer NOT NULL DEFAULT 0,
  riordinato_il     timestamptz
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['blocchi', 'chiarimenti', 'indicazioni', 'stato_albero'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON %I USING (owner_id = app_current_user()) '
      'WITH CHECK (owner_id = app_current_user())', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO diario_app', t);
  END LOOP;
END $$;

-- Gli argomenti delle note già analizzate: non erano salvati, il nome del loro tema li sostituisce.
INSERT INTO blocchi (owner_id, entry_id, ord, argomento, topic_id)
SELECT DISTINCT ON (p.entry_id, p.blocco) p.owner_id, p.entry_id, p.blocco, coalesce(t.label, 'argomento'), p.topic_id
  FROM punti p LEFT JOIN topics t ON t.id = p.topic_id
 WHERE p.parent_id IS NULL
 ORDER BY p.entry_id, p.blocco, p.ord;
