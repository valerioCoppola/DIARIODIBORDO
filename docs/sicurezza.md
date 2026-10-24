# Sicurezza e gestione dei dati

Stato dei requisiti della sezione 8 della specifica dopo l'Incremento 1.

| Requisito | Come | Stato |
|---|---|---|
| Autenticazione obbligatoria | Tutti gli endpoint di contenuto richiedono sessione o token API; pubblici solo `/health` e lo schema della nota | Fatto, testato |
| Autorizzazione lato server e RLS | Ruolo `diario_app` senza BYPASSRLS; RLS forzata su ogni tabella di contenuto; `app.user_id` impostato per transazione | Fatto, testato anche con SQL diretto |
| Storage privato | `data/audio` con permessi 0700/0600, mai servito come statico; audio solo via endpoint autenticato | Fatto, testato |
| Cifratura in transito | HTTPS di Tailscale Serve verso il telefono, rete Tailscale cifrata; su PC il traffico resta su localhost | Da configurare (Tailscale) |
| Cifratura a riposo | Delegata al disco: attiva LUKS sulla partizione che contiene la cartella del progetto | Da verificare sul PC |
| Credenziali | Password Argon2id; token di sessione e API salvati solo come sha256; `.env` con permessi 0600 e fuori dal repository | Fatto |
| Nessuna chiave API nel frontend | Il frontend non contiene segreti; i modelli sono locali | Fatto |
| Protezione CSRF | Cookie SameSite=Strict + header `X-CSRF-Token` obbligatorio per le modifiche | Fatto, testato |
| Tentativi di accesso | Blocco di 15 minuti dopo 8 errori per utente o per IP | Fatto, testato |
| Isolamento tra diari | `diario_id` su ogni tabella di contenuto, politica RLS restrittiva su `app.diario_id`; senza diario nessun contenuto visibile | Fatto, testato anche con SQL diretto |
| Account di altre persone | Solo su invito (codice monouso con scadenza, sha256); password provvisorie da cambiare al primo accesso; disattivazione che revoca sessioni e token; l'amministratore non ha accesso ai contenuti degli altri | Fatto, testato |
| Intestazioni di sicurezza | CSP, X-Frame-Options, nosniff, Referrer-Policy, HSTS dietro HTTPS | Fatto |
| Log senza contenuti personali | Errori dei job solo tecnici; dalle righe di accesso è rimossa la query string | Fatto |
| Deduplicazione e idempotenza | Chiave di idempotenza per sessione, unicità per sha256 | Fatto, testato |
| Conservazione degli originali | File immutabili, verifica con `verify-storage` | Fatto |
| Versionamento delle analisi | Tabella `entry_versions` | Schema pronto (Incremento 2) |
| Backup e ripristino | — | Incremento 6 |
| Esportazione completa e cancellazione | — | Incrementi 3–6 |
| Monitoraggio dei costi | Tabella `model_usage`; con modelli locali il costo è energia e tempo GPU | Schema pronto |

## Collocazione dei dati

Tutti i dati restano sul PC: database in `data/postgres`, audio in
`data/audio`. Tailscale instrada il traffico tra i tuoi dispositivi senza
poterlo leggere (WireGuard end-to-end); vede solo i metadati di connessione
dei dispositivi.

## Cosa resta sul telefono

Finché un vocale non è "Conservato sul PC", il suo audio è nell'archivio del
browser (IndexedDB), accessibile a chi usa il telefono sbloccato. Dopo la
conferma i pezzi audio vengono cancellati; resta solo la riga dell'elenco con
data e durata, rimovibile con "Togli dall'elenco".
