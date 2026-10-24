# Storia dello sviluppo

Aggiornato al 24 ottobre 2026: più diari per persona e più persone, pacchetto pronto per la condivisione.

## Incremento 1 — Fondazioni (completato)

Repository e configurazione, schema completo con RLS, autenticazione,
storage privato, caricamento a blocchi riprendibile e deduplicato, PWA con
registratore e invio robusto, accesso dal telefono via Tailscale
(`scripts/telefono.sh`), avvio automatico (`scripts/installa-servizio.sh`).
Verificato sul PC e su iPhone: registrazione e invio funzionano anche fuori casa.

## Incremento 2 — Elaborazione (questo)

### Creato

- **Worker** (`app/worker.py`, servizio `diario-worker`): coda con `FOR UPDATE SKIP LOCKED` e affitto rinnovato
  da un battito, sveglia via `NOTIFY`, ripresa dalla fase fallita, attesa crescente, errori definitivi,
  servizi non disponibili senza consumo di tentativi.
- **Trascrizione locale** con faster-whisper: scelta automatica di modello e precisione dalla memoria video,
  ripiego sulla CPU se la GPU fallisce, filtro dei silenzi, segmenti con tempi e posizione nel testo.
- **Analisi locale** con Ollama: output vincolato dallo schema, validazione contro `Nota`, correzione guidata
  fino a 3 volte, controlli di aderenza alla fonte (responsabili e scadenze presenti nel testo, ogni azione
  con la frase che la giustifica), vocali lunghi analizzati a parti.
- **Salvataggio**: nota, prima versione in `entry_versions` con modello, prompt e avvisi; argomenti normalizzati
  (`topics`), azioni con citazione e posizione.
- **Indicizzazione**: segmenti con posizione esatta, embedding bge-m3 a 1024 dimensioni.
- **API**: elenco e dettaglio delle note, esportazione JSON canonica, ricerca ibrida, stato delle azioni,
  "riprova" dei job, stato dell'elaborazione.
- **Interfaccia**: archivio con lo stato di ogni vocale, pagina della nota (sintesi, cose da fare da spuntare,
  audio, trascrizione che si riascolta toccando una frase), ricerca con parole evidenziate che apre l'audio nel
  punto giusto. In pausa, **tenere premuto Elimina per 2 secondi** cancella la registrazione dal telefono.
- **Script**: `aggiorna.sh` (applica un aggiornamento: librerie, interfaccia, database, servizi),
  `prepara-ai.sh` (librerie CUDA, Ollama, scelta e download dei modelli, diagnosi), `worker.sh`,
  servizio `diario-worker`, comandi `diagnosi`, `prepara-modelli`, `riprova-errori`. Le migrazioni si applicano
  da sole all'avvio.

### Testato

**77 test automatici del backend** su PostgreSQL 16 + pgvector reali (46 dell'Incremento 1, 31 nuovi), tutti superati.
Whisper e Ollama sono sostituiti da versioni finte; il client Ollama, l'analisi, la validazione, il worker e il
database girano per intero.
- Flusso completo dal caricamento all'archivio: tutti gli stati nell'ordine, nota valida contro lo schema,
  trascrizione intatta, data con l'offset originale, azione con citazione alla posizione esatta, segmenti
  indicizzati a 1024 dimensioni, consumi registrati, richiesta a Ollama con schema, `think: false`, temperatura 0.
- Aderenza alla fonte: responsabile e scadenza inventati rimossi, azione senza riscontro scartata, avvisi salvati.
- Risposte non valide corrette con il messaggio d'errore; titolo fuori misura richiesto di nuovo e poi segnalato.
- Ollama spento: il job aspetta senza consumare tentativi, non riempie lo storico e riparte dall'analisi senza
  ritrascrivere. Modello non scaricato: messaggio con il comando `ollama pull`.
- Tentativi: attesa di 30 s, errore definitivo al quinto, "riprova" dall'interfaccia.
- Nessun parlato: errore definitivo, nessuna nota vuota. Errore di trascrizione: nuovo tentativo e successo.
- Modello che rifiuta il parametro `think`: ripiego automatico.
- Vocale lungo: analisi a parti, azioni raccolte da parti diverse con le loro citazioni.
- Due worker non prendono mai lo stesso job; affitto scaduto: il job torna prelevabile.
- Ricerca: parole evidenziate, forme diverse della stessa parola, ripiego testuale, isolamento tra utenti.
- Isolamento di note, esportazione, azioni e job tra utenti; stato dell'elaborazione.
- Scelta del modello per ogni fascia di memoria video, ripiego GPU → CPU, segmentazione con posizioni esatte.
- **Decodifica dei formati del telefono**: WebM/Opus (Chrome, Android) e MP4/AAC (Safari, iPhone). Questo test
  ha trovato un'incompatibilità reale tra PyAV 19 e faster-whisper 1.2.1, risolta fissando PyAV alla 18.1.

**Processo vero del worker**: avviato come `python -m app.worker`, prende il job, mette in attesa il modello
non scaricabile senza consumare tentativi, registra il battito, si ferma pulito con SIGTERM.

**Prova end-to-end nel browser** (Chromium, microfono simulato, schermo da telefono), superata:
1. In pausa compare Elimina; una pressione breve non elimina; 2 secondi eliminano: niente arriva al PC.
2. Nuova registrazione inviata, elaborata dal worker, nota nell'archivio.
3. Nota con 3 frasi di sintesi, un'azione spuntata (salvata nel database), trascrizione che porta l'audio al punto.
4. Ricerca con parola evidenziata e link alla nota. Modalità chiara e scura. Nessun errore JavaScript.

### Non ancora verificato

- **Interfaccia compilata sul tuo PC**: la cartella `frontend/out` non è scrivibile da qui; la compila
  `scripts/aggiorna.sh`. La stessa build è stata verificata qui (prova end-to-end sopra).
- **Modelli veri sul tuo PC**: qui non si possono scaricare Whisper né i modelli di Ollama. La prima esecuzione
  di `scripts/prepara-ai.sh` (che termina con la diagnosi) è la verifica: GPU vista, modello caricato, analisi
  in JSON valido, embedding a 1024 dimensioni.
- **Qualità delle note su vocali reali** in italiano: va giudicata sull'uso; prompt e modello si possono cambiare
  (`OLLAMA_MODEL` in `.env`).
- Registrazioni molto lunghe (30–60 minuti) su iPhone a schermo bloccato.

## Rifinitura dell'archivio (21 ottobre)

- **File spariti dal PC**: il vocale non compare più (niente "Fermo"); il suo job viene annullato.
  Se il file ricompare, il vocale torna e riprende l'elaborazione. Una nota con l'audio mancante resta, senza lettore.
- **Eliminazione** dalla pagina del vocale (tieni premuto 2 s): audio, copia d'ascolto, nota, versioni,
  trascrizioni, segmenti, cose da fare con il loro storico, job, consumi, argomenti rimasti orfani.
  Funziona anche mentre il worker sta lavorando sul vocale.
- **Ascolto**: copia MP3 a bitrate costante preparata dal worker (o alla prima richiesta). Il WebM del
  registratore non dichiarava la durata (cursore fermo, 0:00); ora durata e "tocca una frase" sono esatti.
  "Audio originale" scarica il file arrivato dal telefono.
- **Archivio**: viste *Più recenti* (per giorno) e *Calendario* (mese a griglia, giorno per giorno, scorrimento),
  raggruppate nel fuso orario del telefono.
- **Test**: 88 automatici (11 nuovi: eliminazione completa, file spariti e ricomparsi, copia d'ascolto da WebM e MP4
  con durata e richieste parziali, calendario nel fuso del telefono); prova nel browser di tutte le funzioni sopra.
- **Corretto**: `scripts/test.sh` cambiava la password del ruolo `diario_app` sul server Postgres condiviso,
  scollegando il Diario in funzione. Ora i test usano quella di `.env`.

## Correzione, mappa, stile (21 ottobre)

- **Correzione della trascrizione** (`PUT /api/v1/entries/{id}/trascrizione`, `app/correzione.py`): nuova versione
  `correzione_manuale`; segmenti audio, citazioni delle azioni e segmenti indicizzati spostati seguendo le differenze
  tra i testi; il job riparte dall'analisi (niente nuova trascrizione). Rifiutata se il testo è cambiato nel frattempo
  o se il worker ci sta lavorando.
- **Mappa** (`GET /api/v1/grafo`, pagina `/mappa/`): argomenti, note e cose da fare con i loro legami; panoramica degli
  argomenti, indice, cose da fare aperte; ogni nodo al centro con i nodi collegati ai lati.
- **Stile**: taccuino in cuoio; caratteri Pinyon Script, Cormorant Garamond, EB Garamond in `frontend/public/fonts`
  (licenza OFL accanto ai file).
- **Test**: 7 nuovi (spostamento delle posizioni, correzione completa con rianalisi, azione spuntata che resta,
  conflitti e isolamento, grafo); tutti i test del backend superati.

## Voci, conoscenza, mappa ad arcipelago (21 ottobre, sera)

- **Chi parla**: pyannote community-1 in locale, in un ambiente a parte (`.venv-voci`, processo separato che
  restituisce tutta la memoria quando finisce). I turni si allineano ai segmenti già trascritti: funziona anche
  sulle note vecchie senza ritrascrivere. Trascrizione divisa per interventi, nomi delle voci modificabili
  (`PUT /api/v1/entries/{id}/voci`); le cose da fare assegnate a una voce seguono il nome. Se non è preparato o
  non riesce, la nota procede senza voci.
- **Cose da fare**: solo impegni espliciti, al massimo 8 parole e 7 per nota; nei vocali lunghi le candidate delle
  parti vengono consolidate (doppioni uniti, non-impegni scartati) mantenendo la citazione. Una rianalisi non
  duplica un'azione già spuntata che cita lo stesso punto.
- **Conoscenza**: aree (al massimo 8, colore fisso) e argomenti con embedding. Il modello riceve aree e argomenti
  esistenti e li riusa; un nome nuovo simile a uno esistente (bge-m3, coseno ≥ 0,86) vi confluisce come alias.
  Argomenti e aree rimasti vuoti spariscono.
- **Mappa**: arcipelago a cerchi annidati (aree come isole, argomenti dentro, grandezza = note), zoom sull'area,
  pagina argomento con le note nel tempo, cose da fare aperte e temi collegati. Palette verificata per
  daltonismo e contrasto su carta e in modalità scura.
- **Rianalisi**: `scripts/rianalizza.sh` (tutte, da capo, con le voci) o `--nota ID`; nessuna nuova trascrizione.
- **Memoria**: contesto dell'analisi 8192 token, Ollama con contesto compresso (q8_0) e un modello alla volta;
  `cli scegli-modello` prova i candidati e tiene il migliore che sta tutto nella scheda video (con 8 GB: qwen3.5:9b,
  poi gemma4:e4b, poi qwen3:8b). La diagnosi dice se il modello finisce in parte nella RAM.
- **Test**: 105 automatici (10 nuovi), build dell'interfaccia, controllo visivo della mappa a schermo di telefono.

## Domande sull'archivio e ricerca (21 ottobre, notte)

- **Chiedi** (`POST /api/v1/chiedi`, risposta NDJSON in streaming): periodo capito dalla domanda ("ieri", "la
  settimana scorsa", "lunedì", "a settembre", nel fuso del telefono), passaggi pertinenti per parole e per
  significato (al massimo 2 per nota), cose da fare aperte se la domanda le riguarda. Una sola chiamata al
  modello; nessuna se non c'è niente di pertinente. Ogni rimando [n] apre la nota nel punto dell'audio.
- **Cerca**: una sola casella. Scrivendo, risultati immediati (argomenti con il colore dell'area, cose da fare,
  note con le parole evidenziate); Invio o "Chiedi", la risposta con le fonti. Filtri per periodo e area,
  domande recenti.
- **Più snello**: tolti la sezione "persone e luoghi" (mai popolata) e la verifica dei JSON importati.
- **Più solido con 8 GB**: l'embedding gira sulla CPU (non scaccia il modello di analisi dalla scheda a ogni
  ricerca); prima di Whisper e pyannote il worker libera la scheda video da Ollama.
- **Test**: 118 automatici.

## La conoscenza che cresce (22 ottobre)

- **Temi con una scheda viva** (migrazione `0004_evoluzione.sql`): ogni tema ha tipo (progetto, idea, tema), una
  sintesi "a che punto è" riscritta a ogni vocale che ne parla e un profilo (embedding di nome + sintesi).
- **Punti** (tabella `punti`): idee, decisioni, domande e fatti di ogni vocale, ciascuno nel suo tema con la frase
  che lo giustifica (senza riscontro: scartato). Un punto quasi uguale già raccolto nel tema non si ripete; un vocale
  successivo può rendere superato un punto (decisione cambiata, domanda risolta).
- **Analisi in tre richieste piccole** (`pipeline/analyze.py`, prompt `analisi-3`): scheda del vocale con 1–3 temi,
  fino a 6 punti e 5 cose da fare; per ogni tema toccato la sintesi aggiornata e i punti superati; se ci sono cose da
  fare aperte vicine a ciò che si dice, quali sono dette fatte (valgono solo se la frase è nel testo e riguarda quella
  cosa). Il modello vede i temi dell'archivio più vicini al vocale con il loro stato.
- **Cose da fare**: 2–6 parole, nel loro tema; un impegno già aperto ripetuto diventa "ribadita"; quelle chiuse da un
  vocale portano il vocale che lo dice. Rianalisi ed eliminazione disfano ciò che un vocale aveva fatto agli altri.
  Rianalizzare un vocale vecchio non riporta indietro la scheda di un tema che ha note più recenti.
- **Trascrizione**: glossario (voci con un nome, temi recenti, aree) passato a Whisper come hotwords; paragrafi a fine
  frase dopo una pausa breve o quando il paragrafo è lungo.
- **Chiedi** legge anche le schede dei temi vicini alla domanda (sintesi, decisioni, domande aperte, cose da fare).
- **Interfaccia**: cose da fare in una riga, raggruppate per tema; scheda del tema (stato, da fare, decisioni, da
  capire, idee, fatti, cambiati, note); temi "in movimento" sulla mappa; nella nota gli appunti e le cose di prima
  chiuse o ripetute. Toccare un appunto o una cosa da fare porta al punto dell'audio.
- **Ritrascrizione**: `scripts/rianalizza.sh --ritrascrivi` rifà trascrizione, voci e analisi di tutte le note
  dall'audio originale, dalla più vecchia, con il glossario preso prima di ripartire da capo. Le trascrizioni corrette
  a mano restano (solo rianalisi) salvo `--anche-corrette`; le cose da fare tenute ritrovano la loro frase.
- **Test**: 129 automatici (11 nuovi), build dell'interfaccia, controllo visivo a schermo di telefono.
- **Dopo l'aggiornamento**: `scripts/aggiorna.sh`, poi `scripts/rianalizza.sh` per ricostruire temi e schede dalla
  nota più vecchia.

## Appunti riorganizzati e temi annidati (22 ottobre, sera)

- **Una sola sezione, Appunti** (migrazione `0005_appunti.sql`, prompt `appunti-1`): l'analisi riorganizza il vocale
  in blocchi, uno per argomento distinto (anche detto di passaggio); in ogni blocco punti in ordine logico con i loro
  dettagli. Ogni punto ha un tipo (decisione, domanda, idea, fatto, da_fare, punto); le cose da fare sono punti
  "da_fare" legati alla casella da spuntare. Spariscono "In breve" e la sezione "Da fare" separata (le tre frasi del
  formato canonico si ricavano dai primi punti). Ogni punto e dettaglio deve trovare riscontro nel testo; se non ne
  trova nessuno la riorganizzazione viene richiesta di nuovo una volta.
- **Collocazione nei temi**: una seconda richiesta, piccola, colloca ogni blocco nell'albero dei temi (numerati, con
  lo stato dei più vicini al vocale): tema esistente, sotto-tema di uno esistente o tema nuovo nella sua area. Nomi di
  tema di 1–3 parole. I temi hanno un padre (`topics.parent_id`); un sotto-tema sta nell'area del suo tema; un tema
  senza note ma con sotto-temi resta.
- **Correzione a mano**: la nuova analisi riparte sempre (prima solo per i job di tipo "audio"); le cose da fare già
  spuntate ritrovano il testo nuovo. Durante l'aggiornamento gli appunti restano visibili ma attenuati.
- **Mappa**: bolle annidate a qualsiasi profondità, nomi a capo dentro la bolla (fino a 3 righe, corpo adattato), nome
  lungo il bordo per le bolle con sotto-temi; avvicinamento su area o tema, tocco fuori per risalire. Scheda del tema
  sotto la mappa: stato, "Dentro" (sotto-temi), da fare, appunti nel tempo, temi collegati. Indice dei temi rientrato.
- **Testo**: le parole con l'apostrofo ("l'orto") si confrontano senza l'articolo.
- **Test**: 131 automatici; build e controllo visivo a schermo di telefono.
- **Dopo l'aggiornamento**: `scripts/aggiorna.sh`, poi `scripts/rianalizza.sh` (o `--ritrascrivi`) per riorganizzare
  tutte le note nel nuovo formato e ricostruire l'albero dei temi dalla nota più vecchia.

## Correzioni (23 ottobre)

- **Appunti**: toccarli non fa più partire l'audio; per riascoltare si tocca la trascrizione. Nella scheda del tema
  gli appunti restano link alla loro nota.
- **Mappa**: ogni livello di bolle ha una tinta più piena del colore dell'area (4 livelli, valori diversi in modalità
  scura) e i sotto-temi hanno un bordo sottile nel colore dell'area, così si staccano dal tema che li contiene. Nome
  leggibile a ogni livello (contrasto ≥ 3,2), livelli vicini distinguibili (≥ 1,2). Il passaggio del dito evidenzia
  solo la bolla toccata, non tutte quelle dentro.
- **Verificato**: tipi e build dell'interfaccia. **Dopo l'aggiornamento**: `scripts/aggiorna.sh` (nessuna rianalisi).

## L'albero che nasce da ciò che si dice (23 ottobre)

Perché: le "aree" erano un livello deciso in anticipo e finivano per contenere categorie ("Progetti") con dentro
singole attività; l'albero cresceva solo verso il basso (il primo vocale metteva in cima un dettaglio e nessun passo
creava poi la cosa più ampia); il modello collocava senza dire quanto era sicuro e nessun controllo lo verificava.

- **Niente aree** (migrazione `0006_albero_vivo.sql`): c'è solo l'albero dei temi; le isole sono i temi in cima.
  Nessuna categoria predefinita né negli esempi del prompt: la struttura nasce solo dai vocali.
- **Collocazione a percorsi** (`pipeline/colloca.py`, prompt `colloca-2`): per ogni argomento un percorso dal tema più
  ampio al più specifico (1–4 passi), con certezza (alta/media/bassa), domanda e alternative. Un tema è una cosa che
  torna, mai un'attività; i nomi di categoria ("progetti", "idee", "attività", "varie"…) sono rifiutati e richiesti di
  nuovo. Un tema in cima può rivelarsi parte di una cosa più ampia e finirci dentro. I passi "nuovi" che hanno il nome
  o un alias di un tema già lì diventano quel tema.
- **Controlli indipendenti dal modello** (`conoscenza.valuta`): somiglianza (bge-m3) tra l'argomento e ciò che il tema
  scelto raccoglie, un altro tema chiaramente più vicino, un tema nuovo quasi uguale a uno esistente, un nome quasi
  uguale (scritto o trascritto male). Abbassano la certezza; soglie in `config.py` (`collocazione_*`, `nome_simile`).
  Un argomento su cui la persona ha dato un'indicazione non passa per i controlli di somiglianza. Quando si ricolloca
  una nota, i temi nati solo da lei non contano come temi già noti.
- **Provvisori e domande**: una collocazione incerta fa nascere temi provvisori (tratteggiati) e, fino a 2 per vocale,
  una domanda (`chiarimenti`) con le opzioni probabili (proposta, alternative, temi più vicini, "a sé"). Con una sola
  opzione si chiede solo se il modello sta indovinando. Un vocale successivo collocato con certezza conferma i temi.
- **Risposte** (`POST /api/v1/chiarimenti/{id}`): una scelta si applica subito, senza modello (il percorso resta
  confermato sul blocco e si riapplica identico anche ricostruendo da capo; "sì, è quello" aggiunge l'alias), poi il
  worker aggiorna le schede (job in modo `evolvi`). Una risposta a parole diventa un'indicazione e la nota viene
  ricollocata (modo `colloca`: gli appunti restano). Le indicazioni (`GET/DELETE /api/v1/indicazioni`) arrivano al
  modello a ogni collocazione e riordino.
- **Correzioni a mano**: `POST /api/v1/temi/{id}/sposta|rinomina|conferma`; il tema diventa "fissato" (il riordino non
  lo tocca) e la correzione diventa un'indicazione. Rinominare con il nome di un tema allo stesso livello li unisce.
- **Riordino** (prompt `riordino-1`): ogni 5 note collocate, o a coda vuota se l'ultimo è più vecchio di 12 ore.
  Raggruppa (il tema nuovo nasce provvisorio), sposta, unisce, rinomina; le modifiche impossibili o su temi fissati
  vengono scartate. `scripts/ricolloca.sh --riordina` lo fa subito.
- **Colori dinamici**: la tinta di ogni tema si calcola dalla forma dell'albero (`conoscenza.tinte`): isole
  distanziate con l'angolo aureo (stabili quando ne nascono altre), sorelle che si dividono in parti uguali una
  fascia di almeno 120° (30° ai livelli sotto). La mappa ci mette la luminosità in OKLCH: più piena scendendo, chiaro e
  scuro alternati tra sorelle. Verificato: contrasto del nome ≥ 5,3 in chiaro e ≥ 4 in scuro; livello dal contenitore
  ΔE OK ≈ 7; sorelle vicine ΔE OK ≈ 5,5.
- **Interfaccia**: "Da chiarire" sulla mappa e sotto gli appunti della nota; scheda del tema con sposta, rinomina,
  "va bene così" per i provvisori; "Cosa mi hai detto" in fondo alla mappa; ricerca e domande filtrate per tema in cima
  (`ramo`) invece che per area.
- **Comandi**: `scripts/ricolloca.sh` (tutte da capo, dalla più vecchia; `--nota ID`; `--riordina`).
- **Test**: 148 automatici (20 nuovi in `test_albero.py`: percorsi, categorie rifiutate, raggruppamento da un
  percorso, domande e risposte, controlli, alias, ricostruzione con percorsi confermati, riordino e temi fissati,
  correzioni a mano, isolamento, colori per ogni numero di sorelle); build dell'interfaccia; prova nel browser a
  schermo di telefono, chiaro e scuro, con risposta e spostamento cliccati.
- **Dopo l'aggiornamento**: `scripts/aggiorna.sh`, poi `scripts/ricolloca.sh` per ricostruire l'albero dalla nota più
  vecchia con la nuova logica (gli appunti restano).

## Più diari, più persone (24 ottobre)

- **Diari** (migrazione `0007_diari_utenti.sql`, `app/account.py`, `routers/diari.py`): ogni contenuto ha `diario_id`
  e una seconda politica RLS restrittiva su `app.diario_id`; `user_tx(utente, diario)`. Archivio, mappa, temi,
  domande, ricerca, Chiedi, cose da fare e riordino dell'albero sono per diario senza toccare le query. Unicità per
  diario (stesso audio o stesso tema in due diari); gli audio nuovi in `data/audio/<utente>/<diario>/`. Il worker
  prende dalla coda anche il diario (`claim_next_job` restituisce `o_diario_id`). Eliminare un diario elimina tutto
  ciò che contiene (ON DELETE CASCADE) e i suoi file; l'ultimo diario non si elimina.
- **Diario aperto** sulla sessione (`POST /api/v1/diari/{id}/apri`, `/chiudi`); `X-Diario` per i caricamenti dal
  telefono e per i token API. Senza diario aperto le API dei contenuti rispondono 409 `nessun_diario`.
- **Persone**: `users.is_admin` (chi c'era diventa amministratore), inviti (`/api/v1/admin/inviti`,
  `/api/v1/auth/registrati`), password provvisorie da cambiare (`users.cambia_password`, 403 finché non cambiata),
  disattivazione, cambio della propria password (`/api/v1/auth/password`, chiude le altre sessioni). Comandi
  `invito`, `utenti`, `disattiva`, `riattiva`, `password-provvisoria`, `rendi-admin`, `diari`, `elimina-utente`.
  `rianalizza`, `ricolloca`, `riordina` lavorano diario per diario (`--diario NOME`).
- **Interfaccia**: scaffale `/diari/` (copertine in sei colori, apertura del libro, nuovo/modifica/elimina), nastro
  segnalibro che tirato giù di poco chiude il diario a quella pagina (riaperto, si torna lì), il cuoio intorno alla
  pagina nel colore del diario, nome del diario sulla copertina; `/registrati/` con invito, `/account/`, `/persone/`
  per l'amministratore. I vocali sul telefono ricordano il loro diario; lo scaffale invia anche quelli rimasti.
- **`scripts/aggiorna.sh`** salva una copia del database in `data/backup/` prima delle migrazioni.
- **Test**: 165 automatici (17 nuovi in `test_diari.py`: isolamento, X-Diario, diario altrui, stesso audio in due
  diari, eliminazione completa, scrittura in un altro diario rifiutata da RLS, inviti, password provvisorie,
  disattivazione, sessioni chiuse al cambio password). Migrazione provata su una copia del database reale (note,
  temi e sessioni nel diario «Diario di Bordo», `updated_at` intatti). Prova nel browser a schermo di telefono,
  chiaro e scuro: scaffale, nuovo diario, chiusura col nastro e riapertura alla stessa pagina, invito e registrazione.
- **Dopo l'aggiornamento**: `scripts/aggiorna.sh` (nessuna rianalisi).

## Rifinitura e pacchetto (24 ottobre, sera)

- **Pagina bianca dopo l'aggiornamento** (solo su browser che avevano già aperto il Diario): le pagine
  uscivano senza `Cache-Control` e il browser riusava l'HTML della versione precedente, che puntava a file
  CSS/JS non più esistenti. Ora le pagine sono `no-cache`, i file con l'impronta in `/_next/static/`
  `immutable`, il service worker (v7) chiede le pagine scavalcando la cache del browser e non conserva le
  risposte di errore. Test: `test_pages_are_always_revalidated`.
- `/favicon.ico` e `/apple-touch-icon.png` serviti dalle icone esistenti (prima 404).
- **Backup completo** (`scripts/backup.sh`: database + audio in un archivio) e **ripristino**
  (`scripts/ripristina.sh`, con conferma e copia di sicurezza prima di sostituire); provati su un
  database di prova: note, diari e audio identici.
- Script con il percorso assoluto del venv (niente più `RuntimeWarning` nei log); `rianalizza.sh` e
  `ricolloca.sh` accettano `--utente`/`--diario` anche senza `--tutte`.
- **Documentazione** riscritta: README con installazione e primi passi, `docs/guida.md` (uso),
  `docs/amministrazione.md` (persone, Tailscale per gli altri, aggiornare, backup, problemi). Esempi
  personali tolti; `.gitignore` per il repository.
- **Licenza** GNU GPL v3 o successiva (`LICENSE`). Nei test i nomi di esempio sono tutti inventati
  (BoscoVivo, Valdirame, Borgoluce).
