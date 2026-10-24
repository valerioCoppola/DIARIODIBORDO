# Architettura e decisioni

## Assunzioni

1. **Tutto in locale sul PC** (Linux, GPU NVIDIA, Ollama già presente). Nessun
   servizio cloud, nessun dato che esce dal PC. Scelta confermata dall'utente.
2. **Più persone, più diari ciascuna** (migrazione `0007_diari_utenti.sql`). Ogni tabella di
   contenuto ha `owner_id` e `diario_id`; due politiche RLS, quella del proprietario e una
   restrittiva sul diario (`app.diario_id`, in AND), fanno sì che una transazione veda solo un
   diario di una persona. Le righe nuove prendono il diario della transazione da sole (DEFAULT).
   Il diario aperto sta sulla sessione; i caricamenti dal telefono indicano il loro con `X-Diario`.
   Gli account nascono da inviti dell'amministratore (codice monouso, salvato come sha256).
3. **Il telefono raggiunge il PC tramite Tailscale**, non esponendo porte su
   Internet. Il PC deve essere acceso per ricevere; nel frattempo il telefono
   conserva i vocali.
4. **Trascrizione e analisi locali** (Incremento 2): faster-whisper sulla GPU
   e un modello via Ollama. Gli embedding a 1024 dimensioni presuppongono un
   modello multilingue come `bge-m3`.

## Scostamenti dallo stack della specifica

La specifica ammette modifiche motivate. Queste lo sono tutte dalla scelta di
restare in locale.

| Specifica | Qui | Motivo |
|---|---|---|
| PostgreSQL su Supabase | PostgreSQL 16 + pgvector in Docker, porta solo su 127.0.0.1 | Locale; stessa tecnologia, schema portabile su Supabase se servisse |
| Supabase Storage privato | Cartella `data/audio`, permessi 0700/0600, percorsi derivati dallo sha256 | Nessun servizio in più da gestire; niente nomi utente nel filesystem |
| Supabase Auth | Autenticazione propria: Argon2id, sessioni via cookie HttpOnly, token API | Supabase Auth richiede il cloud o un'istanza self-hosted pesante |
| Row Level Security di Supabase | RLS nativa di Postgres con `app.user_id` per transazione e un ruolo senza BYPASSRLS | Stessa garanzia: l'isolamento vale anche con un bug nel backend |
| API OpenAI | faster-whisper per la trascrizione, Ollama (qwen3) per l'analisi, bge-m3 per gli embedding | Scelta dell'utente: nessun dato verso terzi |
| Next.js con server Node | Next.js esportato come file statici e servito da FastAPI | Una sola origine e un solo processo: niente CORS, cookie più semplici |
| Coda persistente esterna | La tabella `ingestion_jobs` con `SELECT … FOR UPDATE SKIP LOCKED` | Nessun broker da installare; la coda sta nello stesso backup |

## Flusso di acquisizione

```
telefono                                   PC
─────────────────────────────────          ─────────────────────────────────────────
MediaRecorder → pezzo ogni 5 s
  → IndexedDB (sopravvive a chiusure)
stop → ricompone, sha256 ──────────▶ POST /uploads  (Idempotency-Key = id locale)
                                     ◀── sessione + blocchi_mancanti
per ogni blocco mancante ─────────▶ PUT /uploads/{id}/chunks/{i}  (sha256 del blocco)
                                       scrittura atomica su disco
complete ─────────────────────────▶ POST /uploads/{id}/complete
                                       ricompone, verifica sha256 e dimensione
                                       sposta l'originale in data/audio/…/<sha256>.webm
                                       recordings + ingestion_jobs  [originale_conservato]
◀── conferma
cancella i pezzi locali
```

Ogni passo si può ripetere senza effetti doppi: la sessione è identificata
dalla chiave, i blocchi si sovrascrivono, il completamento restituisce lo
stesso risultato, e lo stesso contenuto (stesso sha256) non crea mai una
seconda registrazione.

Il successo del caricamento è distinto dalla nota archiviata: dopo il
caricamento il job resta in `originale_conservato` finché il worker non lo
porta avanti fino ad `archiviazione_completata` (sotto).

## Modello dati

Migrazione: `backend/migrations/0001_fondazioni.sql`. Tutte le entità della
sezione 5 della specifica sono presenti, più alcune di supporto.

- **recordings**: originale immutabile; `UNIQUE (owner_id, sha256)` per la deduplicazione.
  `recorded_at` solo da fonte affidabile, con la fonte dichiarata e l'offset orario salvato a parte
  (Postgres non conserva l'offset in `timestamptz`).
- **upload_sessions**: caricamenti a blocchi riprendibili.
- **entries**: la nota; `search_tsv` generato con dizionario italiano.
- **entry_versions**: ogni analisi, importazione o correzione, con modello e versione del processo.
- **topics / entry_topics**: vocabolario normalizzato (slug, alias, fusioni) e vista `topic_stats` per frequenza e ricorrenza.
- **entities / entity_mentions / entity_relations**: ogni menzione e relazione punta al passaggio di testo che la giustifica.
- **decisions, action_items, action_item_events**: con fonte, citazione, posizione nel testo; data solo quando determinabile.
- **text_chunks**: segmenti con posizione nel testo originale, `vector(1024)` con indice HNSW e indice full-text.
- **ingestion_jobs / job_events**: coda con stati della sezione 9, tentativi, errori tecnici senza contenuti, storico.
- **model_usage**: consumi dei modelli (anche locali: durate, unità) per il Monitoraggio.

Il tipo `origine_info` (`dichiarato`, `estratto_automatico`, `manuale`)
distingue ciò che è detto esplicitamente da ciò che è stato dedotto da un modello.

## Limiti dei browser mobili per registrazioni lunghe

La specifica chiede di valutarli esplicitamente.

- **Microfono solo in HTTPS.** Per questo l'accesso dal telefono passa da Tailscale Serve, che fornisce un certificato valido.
- **Schermo bloccato o app in background.** Su Android, Chrome di norma continua a registrare con lo schermo spento, ma il sistema può sospendere la scheda. Su iOS, Safari e le PWA possono interrompere il microfono quando la pagina va in background. La pagina richiede il blocco dello schermo acceso (Wake Lock) e salva l'audio ogni 5 secondi: nel caso peggiore si perde l'ultimo pezzo, non la registrazione.
- **Telefonate e altre app che prendono il microfono** chiudono la traccia: la registrazione viene chiusa e salvata con un avviso.
- **Spazio del browser.** La pagina chiede l'archiviazione persistente; a 48 kbit/s un'ora occupa circa 21 MB.
- **Formato.** Chrome e Firefox producono WebM/Opus; Safari produce MP4/AAC. Verificato in Chromium che i pezzi ricomposti, anche dopo un'interruzione, sono un file decodificabile per intero. Su Safari/iOS va verificato sul dispositivo.

**Raccomandazione.** Per vocali lunghi con schermo acceso la PWA è adeguata.
Se serve registrare a schermo bloccato su iOS, la soluzione affidabile è
registrare con l'app di sistema (Memo Vocali) e caricare il file dalla stessa
pagina ("Oppure carica un file audio"), che usa lo stesso invio riprendibile.
Un'app nativa è da valutare solo se questo flusso si rivela scomodo nell'uso
reale.

## Elaborazione (Incremento 2)

Un processo separato, `app.worker` (servizio `diario-worker`), preleva i job
dalla tabella `ingestion_jobs` e li porta fino all'archivio:

```
originale_conservato
  → trascrizione_in_corso → trascrizione_completata   faster-whisper: testo + segmenti temporizzati
  → analisi_in_corso      → dati_strutturati_salvati  Ollama: titolo, categoria, tag, sintesi, azioni
  →                         indicizzazione_completata segmenti di ~900 caratteri + embedding bge-m3
  →                         archiviazione_completata
```

**Processo separato dal servizio web.** La trascrizione occupa la GPU per
minuti: un problema di driver o di memoria non deve fermare la pagina né i
caricamenti. Il worker gira con priorità bassa (`Nice=10`).

**Coda senza broker.** `claim_next_job()` preleva con `FOR UPDATE SKIP LOCKED`
e "affitta" il job (`locked_at`), rinnovato da un battito ogni 20 s: se il
worker muore, dopo 2 minuti il job torna prelevabile. La funzione è
`SECURITY DEFINER` perché il worker usa il ruolo applicativo soggetto a RLS e
non conosce in anticipo il proprietario del prossimo job; restituisce solo id
e proprietario, e tutto il resto avviene in transazioni con `app.user_id`
impostato, quindi con l'isolamento attivo. Un `NOTIFY` sveglia il worker appena
arriva un vocale.

**Ripresa dalla fase fallita.** Ogni fase salva il risultato nella stessa
transazione in cui cambia stato; dopo un errore il job riparte dalla fase in
cui si era fermato (`failed_stage`), senza rifare le precedenti.

**Tre tipi di errore.**
- *Servizio non disponibile* (Ollama spento, modello non scaricato, primo
  download di Whisper senza rete): nuovo tentativo ogni minuto, senza
  consumare tentativi e senza riempire lo storico.
- *Definitivo* (nessun parlato, audio illeggibile, originale mancante): si
  ferma subito, l'originale resta conservato.
- *Altro*: attesa crescente (30 s, 1, 2, 4, 8 min), poi `errore_definitivo` al
  quinto tentativo. Dall'interfaccia ("Riprova") o da riga di comando si
  rimette in coda.

**Trascrizione.** faster-whisper con modello scelto in base alla memoria
video (large-v3 da 6 GB in su, large-v3-turbo sotto o su CPU), filtro dei
silenzi (meno allucinazioni), lingua italiana. Se la GPU fallisce si passa
alla CPU da soli. Sotto i 16 GB di memoria video il modello viene scaricato
dalla GPU dopo ogni vocale, per lasciare spazio al modello di analisi.
`av` è fissato alla 18.1: dalla 19 manca un'opzione usata da faster-whisper
1.2.1 per decodificare l'audio (lo verifica un test con WebM/Opus e MP4/AAC).

**Analisi senza fatti inventati.** Il modello produce solo le parti
interpretative; data e trascrizione vengono dai dati, non dal modello.
L'output è vincolato dallo schema JSON (output strutturato di Ollama), poi
validato: se non va bene il modello riceve l'errore e corregge, fino a tre
volte. Dopo la validazione: responsabili e scadenze non presenti nel testo
vengono tolti, ogni azione deve avere una frase della trascrizione che la
giustifichi (salvata come citazione con la posizione), altrimenti viene
scartata. Gli interventi restano negli avvisi della versione
(`entry_versions.warnings`). I vocali più lunghi del contesto vengono ridotti
ad appunti per parti e la nota viene costruita dagli appunti.

**Ricerca.** Tre classifiche (segmenti per parole con dizionario italiano,
note per titolo e testo, segmenti per significato) fuse con la Reciprocal Rank
Fusion. Se Ollama non risponde la ricerca resta testuale e lo dice.

**Scelta dei modelli.** `scripts/prepara-ai.sh` sceglie il modello di analisi
dalla memoria video (qwen3:14b da 20 GB, qwen3:8b da 8 GB, qwen3:4b sotto o
senza GPU) e lo scrive in `.env`, dove si può cambiare. Qwen3 è multilingue e
regge bene l'italiano; il ragionamento esplicito è disattivato (`think: false`)
perché serve solo il JSON.
