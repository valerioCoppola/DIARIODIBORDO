#!/usr/bin/env bash
# Accesso dal telefono, ovunque tu sia, tramite Tailscale.
#
# Il servizio resta in ascolto solo su 127.0.0.1. Tailscale Serve lo pubblica
# in HTTPS su un indirizzo del tipo https://<pc>.<rete>.ts.net, raggiungibile
# solo dai dispositivi collegati al tuo account Tailscale: a casa, in 4G/5G,
# su qualunque Wi-Fi. Nessuna porta aperta sul router, nessun indirizzo pubblico.
#
#   scripts/telefono.sh              configura (ripetibile) e verifica
#   scripts/telefono.sh --stato      mostra indirizzo e stato, senza cambiare nulla
#   scripts/telefono.sh --disattiva  smette di pubblicare il Diario su Tailscale
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/telefono.sh  come utente normale."
  echo "La password di amministratore viene chiesta solo nei passaggi che la richiedono."
  exit 1
fi
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi
PORT="${PORT:-8000}"
LOCAL="http://127.0.0.1:$PORT"
MODE="${1:-}"

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -rp "   $1 [S/n] " a; [[ ! "${a,,}" =~ ^n ]]; }

health() { curl -fsS --max-time "${2:-4}" "$1/api/v1/health" >/dev/null 2>&1; }

# "<stato>|<nome del PC su MagicDNS>|<domini con certificato HTTPS>"
ts_info() {
  { tailscale status --json 2>/dev/null || true; } | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("NonRaggiungibile||"); sys.exit()
s = d.get("Self") or {}
print("|".join([d.get("BackendState") or "", (s.get("DNSName") or "").rstrip("."),
                ",".join(d.get("CertDomains") or [])]))'
}

# "<FUNNEL se qualcosa è esposto su Internet>|<destinazione pubblicata su https:443 />"
serve_info() {
  { tailscale serve status --json 2>/dev/null || true; } | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin) or {}
except Exception:
    d = {}
funnel = any((d.get("AllowFunnel") or {}).values())
target = ""
for hostport, web in (d.get("Web") or {}).items():
    if hostport.endswith(":443"):
        target = (((web or {}).get("Handlers") or {}).get("/") or {}).get("Proxy", "")
print(("FUNNEL" if funnel else "") + "|" + target)'
}

is_ours() { [[ "$1" == "$LOCAL" || "$1" == "http://localhost:$PORT" ]]; }

show_url() {
  local url="https://$1"
  printf '\n   Indirizzo del Diario:   %s\n\n' "$url"
  if command -v qrencode >/dev/null; then
    qrencode -t ansiutf8 -m 2 "$url"
  else
    echo "   (per un QR code da inquadrare col telefono: sudo apt install qrencode, poi scripts/telefono.sh --stato)"
  fi
}

case "$MODE" in
  ""|--stato|--disattiva) ;;
  *) echo "Uso: scripts/telefono.sh [--stato | --disattiva]"; exit 1 ;;
esac

# ---------------------------------------------------------------- --stato
if [[ "$MODE" == --stato ]]; then
  command -v tailscale >/dev/null || die "Tailscale non è installato: esegui scripts/telefono.sh"
  IFS='|' read -r STATE DNS _ < <(ts_info)
  IFS='|' read -r FUNNEL TARGET < <(serve_info)
  echo "Tailscale sul PC: ${STATE:-sconosciuto}"
  if health "$LOCAL"; then ok "il servizio risponde su $LOCAL"; else warn "il servizio non risponde su $LOCAL"; fi
  if is_ours "$TARGET"; then
    ok "pubblicato nella tua rete Tailscale"
    if health "https://$DNS" 10; then ok "raggiungibile in HTTPS"; else warn "https://$DNS non risponde"; fi
    show_url "$DNS"
  else
    warn "non pubblicato su Tailscale: esegui scripts/telefono.sh"
  fi
  [[ -z "$FUNNEL" ]] || warn "ATTENZIONE: Tailscale Funnel è attivo, qualcosa su questo PC è esposto su Internet (tailscale funnel status)"
  exit 0
fi

# ---------------------------------------------------------------- --disattiva
if [[ "$MODE" == --disattiva ]]; then
  tailscale serve --https=443 off
  ok "Il Diario non è più pubblicato su Tailscale (il servizio sul PC continua a girare)"
  exit 0
fi

# ---------------------------------------------------------------- configurazione
say "1/5  Tailscale sul PC"
if ! command -v tailscale >/dev/null; then
  echo "   Tailscale non è installato: lo installo con lo script ufficiale (https://tailscale.com/install.sh)."
  echo "   Ti verrà chiesta la password di amministratore."
  command -v curl >/dev/null || die "Manca curl: sudo apt install curl"
  curl -fsSL https://tailscale.com/install.sh | sh
fi
VER=$(tailscale version 2>/dev/null | head -1 || true)
[[ "$(printf '%s\n' 1.52.0 "$VER" | sort -V | head -1)" == 1.52.0 ]] \
  || die "Tailscale ${VER:-?} è troppo vecchio (serve 1.52 o successivo): sudo apt update && sudo apt install tailscale"
if command -v systemctl >/dev/null && ! systemctl is-active --quiet tailscaled; then
  echo "   Avvio il servizio tailscaled (e lo abilito all'accensione)"
  sudo systemctl enable --now tailscaled
  sleep 2
fi
ok "Tailscale $VER"

say "2/5  Accesso al tuo account Tailscale"
IFS='|' read -r STATE DNS CERTS < <(ts_info)
if [[ "$STATE" != Running ]]; then
  echo "   Qui sotto compare un link: aprilo e accedi con l'account che userai anche sul telefono."
  sudo tailscale up
  IFS='|' read -r STATE DNS CERTS < <(ts_info)
fi
[[ "$STATE" == Running ]] || die "Tailscale non risulta connesso (stato: ${STATE:-sconosciuto}). Riprova con: sudo tailscale up"
ok "PC connesso alla tua rete Tailscale"

say "3/5  HTTPS nella tua rete Tailscale"
while [[ -z "$DNS" || -z "$CERTS" ]]; do
  warn "Nella tua rete Tailscale mancano MagicDNS o i certificati HTTPS: il microfono del telefono funziona solo in HTTPS."
  echo "   Apri https://login.tailscale.com/admin/dns e attiva:"
  echo "     - MagicDNS"
  echo "     - HTTPS Certificates (pulsante \"Enable HTTPS\")"
  read -rp "   Premi Invio per ricontrollare, oppure scrivi c e Invio per continuare comunque: " A
  [[ "${A,,}" == c ]] && break
  IFS='|' read -r STATE DNS CERTS < <(ts_info)
done
[[ -n "$DNS" ]] || die "Senza MagicDNS il PC non ha un nome HTTPS: attivalo e rilancia lo script"
ok "indirizzo del PC: $DNS"

say "4/5  Pubblico il Diario nella tua rete Tailscale (non su Internet)"
IFS='|' read -r FUNNEL TARGET < <(serve_info)
if [[ -n "$FUNNEL" ]]; then
  warn "Tailscale Funnel è attivo: qualcosa su questo PC è esposto su Internet."
  ask "Azzero le pubblicazioni Tailscale di questo PC e ripubblico solo il Diario, privato?" \
    || die "Interrotto. Controlla con: tailscale funnel status"
  tailscale serve reset
  TARGET=""
fi
if is_ours "$TARGET"; then
  ok "già pubblicato: https://$DNS → $LOCAL"
else
  if [[ -n "$TARGET" ]]; then
    warn "Su https://$DNS è già pubblicato $TARGET"
    ask "Lo sostituisco con il Diario?" || die "Interrotto"
    tailscale serve --https=443 off || true
  fi
  if ! tailscale serve --bg "$LOCAL"; then
    echo "   Autorizzo il tuo utente a gestire Tailscale Serve senza sudo, poi riprovo"
    sudo tailscale set --operator="$USER"
    tailscale serve --bg "$LOCAL"
  fi
  ok "pubblicato: https://$DNS → $LOCAL (solo dispositivi del tuo account; Funnel spento)"
fi

say "5/5  Verifica"
REMOTE_OK=""
if health "$LOCAL"; then
  ok "il servizio risponde su $LOCAL"
  echo "   Controllo l'indirizzo HTTPS (la prima volta Tailscale emette il certificato: può servire mezzo minuto)"
  for _ in 1 2 3 4 5 6; do
    if health "https://$DNS" 10; then REMOTE_OK=1; break; fi
    sleep 5
  done
  if [[ -n "$REMOTE_OK" ]]; then ok "raggiungibile in HTTPS: https://$DNS/api/v1/health"
  else warn "https://$DNS non risponde ancora: riprova tra un minuto con scripts/telefono.sh --stato"; fi
else
  warn "il servizio del Diario non è in esecuzione: l'indirizzo risponderà appena lo avvii"
fi
if command -v systemctl >/dev/null && ! systemctl --user is-enabled --quiet diario 2>/dev/null; then
  warn "il Diario non parte da solo all'accensione: esegui scripts/installa-servizio.sh"
fi

say "Sul telefono"
show_url "$DNS"
cat <<'TXT'
   1. Installa l'app Tailscale (Play Store / App Store) e accedi con lo STESSO account usato qui.
   2. Lasciala sempre attiva, così non devi pensarci:
        Android: Impostazioni → Rete e Internet → VPN → ⚙ accanto a Tailscale → "VPN sempre attiva"
                 (il percorso cambia un po' secondo la marca)
        iPhone:  app Tailscale → Impostazioni → "VPN On Demand"
   3. Apri l'indirizzo qui sopra nel browser e accedi con utente e password del Diario.
   4. Mettilo nella schermata Home (Chrome: menu ⋮ → Aggiungi a schermata Home;
      Safari: Condividi → Aggiungi alla schermata Home) e consenti il microfono.

   Funziona da casa, in 4G/5G e su qualunque Wi-Fi, finché il PC è acceso e online.

   Una cosa da fare una volta sola: in https://login.tailscale.com/admin/machines apri il menu
   "..." accanto a questo PC e scegli "Disable key expiry". Altrimenti fra qualche mese Tailscale
   chiede di riautenticare il PC e da fuori casa non lo raggiungeresti più.
TXT
