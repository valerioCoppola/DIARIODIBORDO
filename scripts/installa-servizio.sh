#!/usr/bin/env bash
# Avvio automatico del Diario: servizio systemd utente che parte all'accensione
# del PC, anche senza fare l'accesso, e si riavvia da solo se si ferma.
# Serve perché il telefono trovi il Diario acceso quando sei fuori casa.
#
#   scripts/installa-servizio.sh            installa (o aggiorna) e avvia
#   scripts/installa-servizio.sh --rimuovi  ferma e rimuove i servizi
#
# Due servizi: diario (pagina e API) e diario-worker (trascrizione e analisi).
# Log: journalctl --user -u diario -f   ·   journalctl --user -u diario-worker -f
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo "Non usare sudo: lancia  scripts/installa-servizio.sh  come utente normale."
  echo "La password di amministratore viene chiesta solo nei passaggi che la richiedono."
  exit 1
fi
cd "$(dirname "$0")/.."
ROOT="$PWD"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
PORT="${PORT:-8000}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/diario.service"

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -rp "   $1 [S/n] " a; [[ ! "${a,,}" =~ ^n ]]; }
health() { curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; }

if [[ "${1:-}" == --rimuovi ]]; then
  systemctl --user disable --now diario diario-worker 2>/dev/null || true
  rm -f "$UNIT" "$UNIT_DIR/diario-worker.service"
  systemctl --user daemon-reload
  ok "Servizio rimosso: il Diario non parte più da solo"
  exit 0
fi
[[ -z "${1:-}" ]] || { echo "Uso: scripts/installa-servizio.sh [--rimuovi]"; exit 1; }

[[ -f .env && -x .venv/bin/uvicorn && -f frontend/out/index.html ]] || die "Prima esegui scripts/setup.sh"
# shellcheck source=scripts/_db.sh
source scripts/_db.sh

say "Database"
if db_ready; then
  ok "risponde su 127.0.0.1:${DB_PORT:-54329}"
else
  echo "   Il database non è in esecuzione: lo avvio con Docker (può chiedere la password di amministratore)"
  DOCKER=(docker); docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
  "${DOCKER[@]}" compose up -d db
  db_wait 90 || die "Il database non risponde. Guarda: sudo docker compose logs db"
  ok "avviato; Docker lo riaccende da solo a ogni avvio del PC"
fi
if [[ "$(systemctl is-enabled docker.service 2>/dev/null || true)" == disabled ]]; then
  echo "   Docker non parte all'accensione: lo abilito"
  sudo systemctl enable docker.service
fi

say "Servizio systemd utente"
if ! systemctl --user is-active --quiet diario && health; then
  warn "Il Diario è già in esecuzione fuori dal servizio (probabilmente scripts/start.sh in un terminale)."
  read -rp "   Fermalo con Ctrl+C in quel terminale, poi premi Invio qui " _
fi
mkdir -p "$UNIT_DIR"
sed "s|@ROOT@|$ROOT|g" scripts/diario.service > "$UNIT"
sed "s|@ROOT@|$ROOT|g" scripts/diario-worker.service > "$UNIT_DIR/diario-worker.service"
systemctl --user daemon-reload
systemctl --user enable diario diario-worker >/dev/null 2>&1
systemctl --user restart diario diario-worker
for _ in $(seq 1 40); do health && break; sleep 1; done
if health; then
  ok "in esecuzione su http://127.0.0.1:$PORT e abilitato all'accensione"
else
  journalctl --user -u diario -n 30 --no-pager || true
  die "Il servizio non risponde: guarda i log qui sopra"
fi
sleep 3
if systemctl --user is-active --quiet diario-worker; then
  ok "elaborazione (trascrizione e analisi) in esecuzione"
else
  journalctl --user -u diario-worker -n 20 --no-pager || true
  warn "il worker dell'elaborazione non è partito: guarda i log qui sopra (prima volta? esegui scripts/prepara-ai.sh)"
fi

say "Avvio anche senza fare l'accesso al PC"
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == yes ]]; then
  ok "già attivo"
else
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
  ok "attivo: dopo un riavvio (anche per un calo di corrente) il Diario riparte da solo"
fi

say "Sospensione automatica"
SCHEMA=org.gnome.settings-daemon.plugins.power
if command -v gsettings >/dev/null && gsettings get "$SCHEMA" sleep-inactive-ac-type >/dev/null 2>&1; then
  if [[ "$(gsettings get "$SCHEMA" sleep-inactive-ac-type)" != "'nothing'" ]]; then
    warn "Collegato alla corrente, il PC va in sospensione dopo un po' di inattività: da fuori casa il Diario non risponderebbe."
    if ask "Disattivo la sospensione automatica quando il PC è alla corrente?"; then
      gsettings set "$SCHEMA" sleep-inactive-ac-type 'nothing'
      ok "disattivata (la riattivi da Impostazioni → Energia)"
    fi
  else
    ok "alla corrente il PC non va in sospensione da solo"
  fi
else
  warn "Controlla nelle impostazioni di energia che il PC non vada in sospensione da solo."
fi

say "Fatto"
echo "   Stato:  systemctl --user status diario      Log:  journalctl --user -u diario -f"
