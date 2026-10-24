# Amministrazione

Per chi ha installato il Diario sul proprio PC: persone, accesso dal telefono, aggiornamenti,
backup, manutenzione e problemi. L'uso quotidiano è in [guida.md](guida.md).

I comandi `scripts/…` si lanciano dalla cartella del progetto, come utente normale (mai con
`sudo`: la password di amministratore viene chiesta solo dove serve). I comandi
`python -m app.cli …` si lanciano dalla cartella `backend/` così:

```bash
cd backend && ../.venv/bin/python -m app.cli utenti
```

## Persone

Chi esegue `scripts/setup.sh` crea il primo account, **amministratore**. Tutti gli altri si
creano l'account da soli con un invito. Ognuno vede solo i propri diari; l'amministratore
gestisce gli account ma dall'app non vede i diari degli altri (l'isolamento è nel database,
con Row Level Security, e vale anche per lui).

Dall'app: scaffale → **Persone**.

<p align="center">
  <img src="img/schermate/persone.jpg" alt="La pagina Persone: inviti e account" width="240">
  <img src="img/schermate/registrati.jpg" alt="Creare l'account con un codice d'invito" width="240">
</p>

| Vuoi… | Nell'app | Da riga di comando |
|---|---|---|
| Invitare qualcuno | *Crea un invito*: codice monouso valido 7 giorni e messaggio pronto da mandare | `app.cli invito --nota "per Marta" [--giorni 7]` |
| Ritirare un invito non usato | *Ritira* accanto all'invito | — |
| Vedere chi c'è | elenco *Account* | `app.cli utenti` |
| Aiutare chi ha dimenticato la password | *Password provvisoria*: gliela dai, al primo accesso ne sceglie una sua | `app.cli password-provvisoria NOME` |
| Bloccare un account | *Disattiva* (tieni premuto): sessioni e token chiusi subito, i diari restano | `app.cli disattiva NOME` |
| Riammetterlo | *Riattiva* | `app.cli riattiva NOME` |
| Dare o togliere il ruolo di amministratore | — | `app.cli rendi-admin NOME [--togli]` |
| Vedere i diari di una persona (solo nomi e numero di note) | — | `app.cli diari NOME` |
| Eliminare un account con tutti i suoi diari e audio | — | `app.cli elimina-utente NOME` (chiede di scrivere il nome) |
| Creare un account senza invito | — | `app.cli create-user NOME [--admin]` |

Il codice d'invito e le password provvisorie si vedono una volta sola: il PC ne conserva solo
l'impronta. Un codice perso si ritira e se ne crea un altro.

Chi ha accesso al PC come amministratore di sistema (database, cartella `data/`) potrebbe
comunque leggere tutto: il Diario è pensato per persone che si fidano di chi tiene il PC.

### Far arrivare gli altri al Diario

Il Diario non è su Internet: i telefoni ci arrivano con **Tailscale** (vedi
[accesso-dal-telefono.md](accesso-dal-telefono.md)). Per una persona con il suo telefono
hai due strade:

- **Condividere il PC** (consigliato): nella console di Tailscale,
  <https://login.tailscale.com/admin/machines>, menu `…` accanto al PC → **Share…** → invita la
  sua email. Lei installa l'app Tailscale con il **suo** account e accetta l'invito: vede solo
  il tuo PC, non gli altri tuoi dispositivi.
- **Usare il tuo account Tailscale** sul suo telefono: più semplice, ma quel telefono vede tutta
  la tua rete Tailscale.

Poi le mandi l'indirizzo `https://<pc>.<rete>.ts.net` (lo stampa `scripts/telefono.sh --stato`)
e il codice d'invito: il messaggio pronto della pagina *Persone* contiene già entrambi.

## Diari

Ogni persona può avere fino a 50 diari; li crea, rinomina ed elimina dallo scaffale.
Tutto ciò che c'era prima dell'introduzione dei diari è nel diario *Diario di Bordo* di chi
c'era. Gli audio nuovi stanno in `data/audio/<persona>/<diario>/`.

Con un token API (per script o Comandi Rapidi) il diario si sceglie con l'header
`X-Diario: <id>` (gli id con `app.cli diari NOME`); senza, si usa l'ultimo diario aperto:

```bash
cd backend && ../.venv/bin/python -m app.cli create-token NOME "comandi-rapidi"
curl -H "Authorization: Bearer $TOKEN" -H "X-Diario: $ID" -F "file=@vocale.m4a" \
     http://localhost:8000/api/v1/recordings
```

## Aggiornare

Dopo aver scaricato una versione nuova del codice (es. `git pull`):

```bash
scripts/aggiorna.sh
```

Aggiorna librerie e interfaccia, **salva una copia del database** in `data/backup/`, applica le
migrazioni e riavvia i servizi. Se la copia non riesce si ferma prima di toccare lo schema.
Sui telefoni basta ricaricare la pagina (o chiudere e riaprire l'app).

## Backup e ripristino

```bash
scripts/backup.sh                    # data/backup/diario-completo-AAAAMMGG-HHMMSS.tar.gz
scripts/backup.sh /media/disco       # su un disco esterno
scripts/ripristina.sh FILE.tar.gz    # sostituisce database e audio con quelli della copia
```

La copia contiene database (persone, diari, note, temi) e audio originali; non contiene `.env`
né i modelli. Contiene tutti i vocali: tienila al sicuro. Per un backup automatico ogni notte:

```bash
crontab -e
# aggiungi la riga (sostituisci il percorso):
30 3 * * * /percorso/del/progetto/scripts/backup.sh /media/disco >/dev/null 2>&1
```

**Spostare il Diario su un altro PC**: sul nuovo PC clona il progetto ed esegui
`scripts/setup.sh` (crea pure un utente qualsiasi: verrà sostituito), poi
`scripts/ripristina.sh` con la copia, poi `scripts/prepara-ai.sh` e `scripts/telefono.sh`.

## Elaborazione e modelli

| Comando | A cosa serve |
|---|---|
| `scripts/prepara-ai.sh` | Installa Ollama e le librerie GPU, sceglie il modello che sta nella scheda video, prepara il riconoscimento delle voci, verifica |
| `app.cli diagnosi` | Controlla GPU, modelli, Ollama ed elaborazione e dice cosa sistemare |
| `app.cli riprova-errori NOME` | Rimette in coda i vocali fermi per errore |
| `scripts/rianalizza.sh` | Rifà l'analisi di tutte le note dalle trascrizioni salvate, diario per diario, dalla più vecchia (`--nota ID`, `--diario NOME`, `--utente NOME`, `--ritrascrivi`) |
| `scripts/ricolloca.sh` | Rifà solo la collocazione nell'albero dei temi (`--nota ID`, `--diario NOME`, `--riordina`) |

Il modello di analisi si cambia in `.env` (`OLLAMA_MODEL=…` e `OLLAMA_MODEL_FISSO=1`), poi
`systemctl --user restart diario-worker`.

## Servizi e log

| Comando | A cosa serve |
|---|---|
| `systemctl --user status diario diario-worker` | Stato di servizio web ed elaborazione |
| `journalctl --user -u diario -f` | Log del servizio web |
| `journalctl --user -u diario-worker -f` | Log dell'elaborazione |
| `scripts/installa-servizio.sh` / `--rimuovi` | Avvio automatico all'accensione |
| `scripts/start.sh`, `scripts/worker.sh` | Avvio in primo piano, senza avvio automatico |
| `scripts/telefono.sh --stato` | Indirizzo per il telefono e stato di Tailscale |
| `app.cli verify-storage NOME` | Ricalcola le impronte di tutti gli audio originali |
| `scripts/test.sh` | Test automatici su un database temporaneo (non tocca i dati) |

## Problemi

| Sintomo | Cosa fare |
|---|---|
| Pagina bianca o senza stile dopo un aggiornamento | Il browser aveva la pagina vecchia: ricarica (sul PC Ctrl+Maiusc+R, sul telefono chiudi e riapri l'app). Dalla versione con i diari le pagine si ricontrollano sempre e non succede più |
| *Il PC non risponde* dal telefono | `scripts/telefono.sh --stato`; Tailscale attivo sul telefono? PC acceso e non sospeso? |
| I vocali restano *In coda* | `systemctl --user status diario-worker`, poi `app.cli diagnosi` |
| *Modello non scaricato* negli errori | `scripts/prepara-ai.sh` |
| Una persona non riesce a entrare | Account disattivato o password dimenticata: *Persone* → *Riattiva* o *Password provvisoria*. Dopo 8 tentativi sbagliati l'accesso si blocca per 15 minuti |
| *Database non raggiungibile* | `docker ps -a` (container `diario-db`), `docker compose up -d db` |
| `scripts/aggiorna.sh` si ferma alla copia del database | Spazio su disco o permessi su Docker: `df -h`, `docker ps` |
