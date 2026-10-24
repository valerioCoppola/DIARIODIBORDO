# Diario di Bordo

Un diario vocale personale che vive sul tuo PC. Registri dal telefono, l'audio arriva al PC,
viene trascritto e riorganizzato in appunti, cose da fare e una mappa dei temi che cresce con
ciò che racconti. Poi puoi cercare o fare domande in italiano ("cosa ho deciso col
commercialista?"). **Tutto in locale**: trascrizione e analisi girano sul PC, nessun dato
lascia casa tua.

- **Registra dal telefono** come app (PWA): salvataggio ogni 5 secondi, invio che riprende da
  solo se la rete cade, nulla si perde se la pagina si chiude.
- **Note ordinate da sole**: trascrizione (faster-whisper), appunti per argomento, decisioni,
  domande aperte, cose da fare con la frase che le giustifica, chi parla (pyannote).
- **Una mappa dei temi** che nasce da ciò che dici, senza categorie decise in anticipo; quando
  non è sicuro, chiede.
- **Cerca o chiedi**: ricerca per parole e per significato, risposte con i rimandi al punto
  esatto dell'audio.
- **Più diari** per persona (*Lavoro*, *Viaggio*, *Idee*…), ciascuno separato dagli altri,
  su uno scaffale di quaderni di cuoio; si chiudono tirando giù il nastro segnalibro.
- **Più persone** sullo stesso PC, su invito: ognuno vede solo i suoi diari.
- **Raggiungibile ovunque** con Tailscale, senza aprire porte sul router.

```
 telefono (PWA)  ──HTTPS via Tailscale──▶  PC: FastAPI (127.0.0.1:8000) + interfaccia Next.js
   registra, salva a pezzi,                   ├─▶ PostgreSQL 16 + pgvector (Docker, solo localhost)
   invia a blocchi                            └─▶ data/audio/  originali, permessi 0600

 worker ── coda in PostgreSQL ──▶ trascrizione faster-whisper (GPU o CPU)
                                ▶ analisi      Ollama (modello scelto in base alla scheda video)
                                ▶ indice       embedding bge-m3 per la ricerca per significato
```

## Requisiti

- **Linux** (sviluppato e provato su Ubuntu) con **Docker Engine** e il plugin `docker compose`
- **Python 3.11+** e **Node.js 20+**
- Consigliata una **GPU NVIDIA** (8 GB bastano) con i driver installati; senza, si usa la CPU,
  più lentamente
- Circa **10 GB** di spazio per i modelli
- Per usarlo dal telefono: **[Tailscale](https://tailscale.com)** (gratuito per uso personale)
  su PC e telefono

Ollama, le librerie GPU e i modelli li installa lo script `scripts/prepara-ai.sh`.

## Installazione

```bash
git clone <indirizzo-del-repository> diario-di-bordo
cd diario-di-bordo
scripts/setup.sh
```

`setup.sh` fa tutto in ordine e alla fine propone i passi successivi; si può rilanciare quando
si vuole (salta quello che è già fatto):

1. crea `.env` con password casuali, avvia il database in Docker, prepara l'ambiente Python,
   compila l'interfaccia;
2. ti chiede **nome utente e password**: sei l'amministratore;
3. **elaborazione** (`scripts/prepara-ai.sh`): Ollama, librerie GPU, scelta e download dei
   modelli, verifica finale;
4. **avvio automatico** all'accensione del PC (`scripts/installa-servizio.sh`);
5. **accesso dal telefono** con Tailscale (`scripts/telefono.sh`): stampa l'indirizzo
   `https://<pc>.<rete>.ts.net` (e un QR code, se è installato `qrencode`).

Ognuno dei passi 3–5 si può fare anche dopo, lanciando lo script corrispondente. Nessuno va
lanciato con `sudo`: la password di amministratore viene chiesta solo dove serve.

## Primi passi

1. **Sul PC** apri <http://localhost:8000>; **sul telefono** l'indirizzo `https://…ts.net`
   (con l'app Tailscale attiva, stesso account del PC). Entra con utente e password.
2. Sul telefono **aggiungi il Diario alla schermata Home** e consenti il microfono.
3. Sullo **scaffale** tocca *Diario di Bordo*, il tuo primo diario.
4. Tocca il **pulsante rosso**, parla, toccalo di nuovo. Dopo qualche minuto la nota è pronta
   nell'**Archivio**; i temi compaiono sulla **Mappa**.
5. Per tornare allo scaffale **tira giù il nastro rosso** in cima alla pagina.
6. Vuoi far entrare qualcuno? Scaffale → **Persone** → *Crea un invito*.

La guida completa è in **[docs/guida.md](docs/guida.md)**.

## Documentazione

| | |
|---|---|
| [docs/guida.md](docs/guida.md) | **Guida all'uso**: scaffale e diari, registrare, archivio, note, mappa, cerca, account |
| [docs/amministrazione.md](docs/amministrazione.md) | **Per chi gestisce il PC**: persone e inviti, aggiornare, backup e ripristino, modelli, log, problemi |
| [docs/accesso-dal-telefono.md](docs/accesso-dal-telefono.md) | Tailscale passo per passo, restare raggiungibili fuori casa |
| [docs/architettura.md](docs/architettura.md) | Scelte tecniche, modello dati, pipeline di elaborazione |
| [docs/sicurezza.md](docs/sicurezza.md) | Autenticazione, isolamento tra persone e tra diari, dove stanno i dati |
| [docs/stato.md](docs/stato.md) | Storia dello sviluppo, cosa è stato provato |

## Comandi principali

| Comando | A cosa serve |
|---|---|
| `scripts/setup.sh` | Prima installazione (ripetibile) |
| `scripts/aggiorna.sh` | Dopo aver aggiornato il codice: librerie, interfaccia, copia del database, migrazioni, riavvio |
| `scripts/backup.sh` / `scripts/ripristina.sh FILE` | Copia completa (database + audio) e ripristino |
| `scripts/prepara-ai.sh` | Modelli di trascrizione, analisi e voci |
| `scripts/telefono.sh --stato` | Indirizzo per il telefono e stato di Tailscale |
| `systemctl --user status diario diario-worker` | Stato di servizio ed elaborazione |
| `scripts/test.sh` | Test automatici (database temporaneo, non tocca i dati) |

Tutti gli altri, compresa la gestione degli utenti da riga di comando, in
[docs/amministrazione.md](docs/amministrazione.md).

## Dove stanno i dati

Tutto nella cartella del progetto, sul PC: database in `data/postgres`, audio originali in
`data/audio`, copie in `data/backup`. `.env` contiene le password del database. Nessuno di
questi file finisce nel repository (`.gitignore`). Il servizio ascolta solo su `127.0.0.1`;
il telefono passa da Tailscale, che cifra il traffico e fornisce l'HTTPS. Per la cifratura a
riposo usa un disco cifrato (LUKS). Dettagli in [docs/sicurezza.md](docs/sicurezza.md).

## Struttura

```
backend/
  app/            FastAPI: configurazione, database, sicurezza, account e diari, acquisizione,
                  archivio, ricerca, domande, conoscenza (temi), router delle API
  app/pipeline/   trascrizione (faster-whisper), analisi e collocazione (Ollama), testo
  app/worker.py   coda dei job: fasi, ripresa, tentativi
  migrations/     SQL numerato, applicato da `app.cli migrate` (anche all'avvio)
  tests/          pytest su PostgreSQL reale
frontend/
  app/            pagine: scaffale, registra, archivio, nota, mappa, cerca, accesso, account, persone
  components/     copertina e nastro, mappa ad arcipelago, calendario, lettore…
  lib/            registratore, archivio locale (IndexedDB), invio riprendibile, diario aperto
docs/             guide e documentazione tecnica
scripts/          installazione, avvio, aggiornamento, backup, servizi systemd, Tailscale
```

API documentate su <http://localhost:8000/api/docs> e in [docs/openapi.json](docs/openapi.json);
schema JSON della nota in [docs/nota.schema.json](docs/nota.schema.json).

## Licenza

Diario di Bordo è software libero: puoi usarlo, studiarlo, modificarlo e ridistribuirlo secondo
la **GNU General Public License versione 3** o successiva ([LICENSE](LICENSE)). Chi distribuisce
una versione modificata deve renderne disponibile il codice con la stessa licenza.

I caratteri Pinyon Script, Cormorant Garamond ed EB Garamond, inclusi in
`frontend/public/fonts`, hanno la loro licenza (SIL Open Font License), accanto ai file.
