# shellcheck shell=bash
# Funzioni comuni sul database, incluse da start.sh e installa-servizio.sh.
# Richiedono ROOT (cartella del progetto) e le variabili di .env già esportate.

# Vero se il database accetta connessioni con le credenziali del servizio.
db_ready() {
  "$ROOT/.venv/bin/python" -c '
import os, psycopg
psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=3).close()' >/dev/null 2>&1
}

# Attende il database fino a $1 secondi (predefinito 120).
db_wait() {
  local i
  for ((i = 0; i < ${1:-120}; i += 2)); do db_ready && return 0; sleep 2; done
  db_ready
}
