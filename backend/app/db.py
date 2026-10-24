"""Accesso al database.

Ogni operazione sui contenuti avviene in una transazione che imposta
`app.user_id` e `app.diario_id` con set_config(..., true): i valori valgono solo
per quella transazione, quindi le connessioni del pool non si portano dietro
l'utente o il diario precedente. Le politiche RLS del DB fanno il resto: si
vedono solo le righe di quella persona, in quel diario.
"""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger("diario.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def system_tx() -> Iterator[psycopg.Connection]:
    """Transazione senza utente: vede solo le tabelle di autenticazione."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def user_tx(user_id: UUID | str, diario_id: UUID | str | None = None) -> Iterator[psycopg.Connection]:
    """Transazione con RLS attiva per l'utente e il diario indicati.

    Senza diario si vedono solo i diari della persona (per sceglierne uno), nessun contenuto.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.user_id', %s, true), set_config('app.diario_id', %s, true)",
                         (str(user_id), str(diario_id) if diario_id else ""))
            yield conn


# ---------------------------------------------------------------------------
# Migrazioni
# ---------------------------------------------------------------------------

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def list_migrations() -> list[Path]:
    files = sorted(p for p in MIGRATIONS_DIR.iterdir() if _MIGRATION_RE.match(p.name))
    return files


def run_migrations(admin_url: str | None = None, app_password: str | None = None) -> list[str]:
    """Applica le migrazioni mancanti, in ordine, ciascuna nella propria transazione.

    Registra nome e sha256 di ogni file: se un file già applicato viene
    modificato, il runner si ferma invece di ignorare la differenza.
    """
    settings = get_settings()
    admin_url = admin_url or settings.database_admin_url
    app_password = app_password if app_password is not None else settings.db_app_password
    applied: list[str] = []

    with psycopg.connect(admin_url, autocommit=False) as conn:
        # Servizio e worker all'avvio migrano entrambi: uno alla volta.
        conn.execute("SELECT pg_advisory_lock(727274)")
        conn.commit()
        with conn.transaction():
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       name       text PRIMARY KEY,
                       sha256     text NOT NULL,
                       applied_at timestamptz NOT NULL DEFAULT now())"""
            )
        done = {r[0]: r[1] for r in conn.execute("SELECT name, sha256 FROM schema_migrations")}

        for path in list_migrations():
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise RuntimeError(
                        f"La migrazione {path.name} è stata modificata dopo l'applicazione. "
                        "Creare una nuova migrazione invece di modificarla."
                    )
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            applied.append(path.name)
            log.info("migrazione applicata: %s", path.name)

        # Il ruolo applicativo può accedere solo con la password configurata.
        with conn.transaction():
            conn.execute(
                pgsql.SQL("ALTER ROLE diario_app LOGIN PASSWORD {}").format(
                    pgsql.Literal(app_password)
                )
            )
            dbname = conn.info.dbname
            conn.execute(
                pgsql.SQL("GRANT CONNECT ON DATABASE {} TO diario_app").format(
                    pgsql.Identifier(dbname)
                )
            )
    return applied
