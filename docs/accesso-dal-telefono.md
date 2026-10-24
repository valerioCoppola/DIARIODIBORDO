# Accesso dal telefono, ovunque tu sia

Il Diario gira sul PC e ascolta solo su `127.0.0.1:8000`: nessuna porta aperta
sul router, nessun indirizzo pubblico. Il telefono ci arriva con **Tailscale**,
una rete privata cifrata tra i tuoi dispositivi: a casa, in 4G/5G o su
qualunque Wi-Fi apri lo stesso indirizzo, del tipo

    https://<nome-pc>.<rete>.ts.net

Lo raggiungono solo i dispositivi collegati al tuo account Tailscale, e
Tailscale fornisce anche l'HTTPS che il browser richiede per il microfono.

## Configurazione: un comando sul PC

```bash
cd diario-di-bordo            # la cartella del progetto
scripts/installa-servizio.sh   # il Diario parte da solo all'accensione e resta attivo
scripts/telefono.sh            # Tailscale: installazione, accesso, HTTPS, pubblicazione, verifica
```

`scripts/telefono.sh` fa, saltando quello che è già fatto:

1. installa Tailscale se manca (script ufficiale, chiede la password di amministratore);
2. collega il PC al tuo account (stampa un link da aprire per l'accesso);
3. controlla che nella rete Tailscale siano attivi MagicDNS e i certificati
   HTTPS; se mancano ti dice dove attivarli
   (<https://login.tailscale.com/admin/dns>) e aspetta;
4. pubblica il Diario con `tailscale serve` **solo nella tua rete**: se trova
   Tailscale Funnel attivo (esposizione su Internet) si ferma e chiede;
5. verifica che `https://…ts.net/api/v1/health` risponda e stampa
   l'indirizzo, con un QR code se è installato `qrencode`.

La configurazione resta salvata in Tailscale: dopo un riavvio del PC non va rifatta.

Altri comandi:

| Comando | A cosa serve |
|---|---|
| `scripts/telefono.sh --stato` | Indirizzo, QR code e stato, senza cambiare nulla |
| `scripts/telefono.sh --disattiva` | Smette di pubblicare il Diario su Tailscale |
| `scripts/installa-servizio.sh --rimuovi` | Toglie l'avvio automatico |

## Sul telefono

1. Installa l'app **Tailscale** e accedi con lo **stesso account** usato sul PC.
2. Lasciala sempre attiva:
   - Android: Impostazioni → Rete e Internet → VPN → ⚙ accanto a Tailscale → **VPN sempre attiva**
   - iPhone: app Tailscale → Impostazioni → **VPN On Demand**
3. Apri l'indirizzo `https://…ts.net` nel browser e accedi con utente e password del Diario.
4. Mettilo nella schermata Home (Chrome: menu ⋮ → Aggiungi a schermata Home;
   Safari: Condividi → Aggiungi alla schermata Home) e consenti il microfono.

## Altre persone

Ognuno con il proprio telefono e il proprio account del Diario (vedi *Persone* in
[amministrazione.md](amministrazione.md)). Per farle arrivare al PC: nella console di Tailscale
<https://login.tailscale.com/admin/machines>, menu `…` accanto al PC → **Share…** → la sua email.
Installa l'app Tailscale con il suo account e accetta: vede solo il tuo PC. Poi apre lo stesso
indirizzo `https://…ts.net` e si crea l'account con il codice d'invito.

## Perché resti raggiungibile quando sei fuori

Da fuori casa nessuno può riaccendere il PC o fare l'accesso, quindi:

- **Avvio automatico**: `scripts/installa-servizio.sh` installa un servizio
  systemd utente con *linger*: dopo un riavvio, anche per un calo di corrente,
  il Diario riparte senza che nessuno faccia l'accesso. Tailscale e Docker
  partono già da soli all'accensione.
- **Niente sospensione**: lo script propone di disattivare la sospensione
  automatica quando il PC è alla corrente. Per esserne certi anche dalla
  schermata di login, dopo un riavvio:
  `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
  (per annullare: lo stesso comando con `unmask`).
- **Scadenza della chiave Tailscale**: per sicurezza Tailscale chiede di
  riautenticare ogni dispositivo dopo alcuni mesi. Per il PC, in
  <https://login.tailscale.com/admin/machines>, apri il menu `…` accanto al PC
  e scegli **Disable key expiry**.
- **Disco cifrato con LUKS**: dopo un riavvio il PC aspetta la passphrase
  all'avvio. In quel caso il Diario torna disponibile solo quando la inserisci.

## Se non funziona

| Sintomo | Causa probabile |
|---|---|
| "Il microfono richiede una connessione HTTPS" | Stai usando un IP o `http://`: usa l'indirizzo `https://…ts.net` |
| La pagina non si apre dal telefono | Tailscale spento sul telefono, oppure account diverso da quello del PC |
| "Il PC non risponde" | PC spento o sospeso, oppure servizio fermo: `scripts/telefono.sh --stato`, `systemctl --user status diario` |
| Funzionava, ora non più da fuori | Chiave Tailscale del PC scaduta: riaccedi con `sudo tailscale up` e disattiva la scadenza |
| L'accesso riesce ma ogni azione dà errore 403 | Pagina aperta prima di un cambio di password: ricarica e accedi di nuovo |
| Un dispositivo fuori dal tuo account apre l'indirizzo | Non dovrebbe succedere: controlla `tailscale funnel status` e che il Funnel sia spento |
