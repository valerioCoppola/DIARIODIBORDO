"""Persone e diari: account, inviti, password provvisorie, diari di ciascuno.

L'amministratore (chi ha installato il Diario) crea gli inviti; chi riceve un codice
si crea da solo l'account con nome utente e password. L'amministratore può
disattivare un account o dargli una password provvisoria, che va cambiata al primo
accesso. Non vede i diari degli altri: le politiche RLS valgono anche per lui.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import psycopg

from . import storage
from .db import system_tx, user_tx
from .security import hash_password, token_digest

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,64}$")
PASSWORD_MIN = 12
COPERTINE = ("cuoio", "notte", "bosco", "vino", "ardesia", "senape")
PRIMO_DIARIO = "Diario di Bordo"
INVITO_GIORNI = 7
# Codice da dettare o scrivere a mano: niente lettere che si confondono (0/O, 1/I/L).
_ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class AccountError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def normalizza_username(username: str) -> str:
    u = username.strip().lower()
    if not USERNAME_RE.match(u):
        raise AccountError(422, "nome_utente_non_valido",
                           "Il nome utente va da 3 a 64 caratteri: lettere minuscole, cifre, punto, trattino")
    return u


def controlla_password(password: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise AccountError(422, "password_corta", f"La password deve avere almeno {PASSWORD_MIN} caratteri")


def password_provvisoria() -> str:
    return "-".join("".join(secrets.choice(_ALFABETO.lower()) for _ in range(4)) for _ in range(4))


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def crea_utente(conn: psycopg.Connection, username: str, password: str, is_admin: bool = False) -> UUID:
    """Crea l'account con il suo primo diario. Va chiamata dentro system_tx."""
    u = normalizza_username(username)
    controlla_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username = %s", (u,)).fetchone():
        raise AccountError(409, "nome_utente_occupato", "Questo nome utente esiste già")
    uid = conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s) RETURNING id",
                       (u, hash_password(password), is_admin)).fetchone()["id"]
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
    conn.execute("INSERT INTO diari (owner_id, nome) VALUES (%s, %s)", (uid, PRIMO_DIARIO))
    conn.execute("SELECT set_config('app.user_id', '', true)")
    return uid


def imposta_password(conn: psycopg.Connection, user_id: UUID, password: str, provvisoria: bool,
                     tranne_sessione: UUID | None = None) -> None:
    """Nuova password; le sessioni aperte si chiudono (tranne quella da cui la si cambia)."""
    controlla_password(password)
    conn.execute("UPDATE users SET password_hash = %s, cambia_password = %s WHERE id = %s",
                 (hash_password(password), provvisoria, user_id))
    conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL "
                 "AND id IS DISTINCT FROM %s", (user_id, tranne_sessione))


def elenco_utenti() -> list[dict]:
    with system_tx() as conn:
        return conn.execute(
            """SELECT u.id, u.username, u.is_admin, u.disabled, u.cambia_password, u.created_at, u.last_login_at,
                      (SELECT max(s.last_seen_at) FROM sessions s WHERE s.user_id = u.id) AS visto_il
                 FROM users u ORDER BY u.created_at""").fetchall()


def imposta_attivo(admin_id: UUID, user_id: UUID, attivo: bool) -> None:
    if user_id == admin_id:
        raise AccountError(409, "se_stesso", "Non puoi disattivare il tuo account")
    with system_tx() as conn:
        row = conn.execute("UPDATE users SET disabled = %s WHERE id = %s RETURNING id",
                           (not attivo, user_id)).fetchone()
        if row is None:
            raise AccountError(404, "utente_inesistente", "Utente non trovato")
        if not attivo:
            conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
            conn.execute("UPDATE api_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                         (user_id,))


def reimposta_password(admin_id: UUID, user_id: UUID) -> str:
    """Password provvisoria, mostrata una volta all'amministratore: al primo accesso va cambiata."""
    if user_id == admin_id:
        raise AccountError(409, "se_stesso", "La tua password la cambi da «Il mio account»")
    nuova = password_provvisoria()
    with system_tx() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            raise AccountError(404, "utente_inesistente", "Utente non trovato")
        imposta_password(conn, user_id, nuova, provvisoria=True)
    return nuova


# ---------------------------------------------------------------------------
# Inviti
# ---------------------------------------------------------------------------


def _codice() -> str:
    return "-".join("".join(secrets.choice(_ALFABETO) for _ in range(4)) for _ in range(3))


def _norm_codice(codice: str) -> str:
    c = re.sub(r"[^A-Z0-9]", "", codice.upper())
    return "-".join(c[i:i + 4] for i in range(0, len(c), 4))


def crea_invito(admin_id: UUID | None, nota: str | None, giorni: int = INVITO_GIORNI) -> tuple[str, datetime]:
    codice = _codice()
    scade = datetime.now(timezone.utc) + timedelta(days=giorni)
    with system_tx() as conn:
        conn.execute("INSERT INTO inviti (codice_hash, nota, creato_da, scade_il) VALUES (%s, %s, %s, %s)",
                     (token_digest(codice), (nota or "").strip()[:80] or None, admin_id, scade))
    return codice, scade


def elenco_inviti() -> list[dict]:
    with system_tx() as conn:
        return conn.execute(
            """SELECT i.id, i.nota, i.created_at, i.scade_il, i.usato_il, i.revocato_il, u.username AS usato_da
                 FROM inviti i LEFT JOIN users u ON u.id = i.usato_da
                WHERE i.usato_il IS NOT NULL OR (i.revocato_il IS NULL AND i.scade_il > now() - interval '7 days')
                ORDER BY i.created_at DESC LIMIT 50""").fetchall()


def revoca_invito(invito_id: UUID) -> None:
    with system_tx() as conn:
        conn.execute("UPDATE inviti SET revocato_il = now() WHERE id = %s AND usato_il IS NULL", (invito_id,))


def registra(codice: str, username: str, password: str) -> UUID:
    """Crea l'account con un invito valido, che da quel momento non vale più."""
    with system_tx() as conn:
        inv = conn.execute(
            "SELECT id FROM inviti WHERE codice_hash = %s AND usato_il IS NULL AND revocato_il IS NULL "
            "AND scade_il > now() FOR UPDATE", (token_digest(_norm_codice(codice)),)).fetchone()
        if inv is None:
            raise AccountError(403, "invito_non_valido", "Codice d'invito non valido, già usato o scaduto")
        uid = crea_utente(conn, username, password)
        conn.execute("UPDATE inviti SET usato_da = %s, usato_il = now() WHERE id = %s", (uid, inv["id"]))
    return uid


# ---------------------------------------------------------------------------
# Diari
# ---------------------------------------------------------------------------


def _nome(nome: str) -> str:
    n = " ".join(nome.split())
    if not 1 <= len(n) <= 40:
        raise AccountError(422, "nome_non_valido", "Il nome del diario va da 1 a 40 caratteri")
    return n


def _copertina(c: str | None) -> str:
    if c is not None and c not in COPERTINE:
        raise AccountError(422, "copertina_non_valida", "Copertina sconosciuta")
    return c or "cuoio"


def diario_out(r: dict) -> dict:
    return {"id": r["id"], "nome": r["nome"], "copertina": r["copertina"], "creato_il": r["created_at"],
            "aperto_il": r["aperto_il"]}


def elenco_diari(user_id: UUID) -> list[dict]:
    """I diari della persona, il più usato di recente per primo, con quante note contengono."""
    with user_tx(user_id) as conn:
        diari = conn.execute("SELECT * FROM diari ORDER BY aperto_il DESC NULLS LAST, created_at").fetchall()
    out = []
    for d in diari:
        with user_tx(user_id, d["id"]) as conn:
            n = conn.execute("SELECT count(*) AS n, max(coalesce(data_ora, created_at)) AS ultima FROM entries "
                             "WHERE deleted_at IS NULL").fetchone()
        out.append({**diario_out(d), "note": n["n"], "ultima_nota": n["ultima"]})
    return out


def crea_diario(user_id: UUID, nome: str, copertina: str | None) -> dict:
    with user_tx(user_id) as conn:
        if conn.execute("SELECT count(*) AS n FROM diari").fetchone()["n"] >= 50:
            raise AccountError(409, "troppi_diari", "Hai già 50 diari")
        return diario_out(conn.execute(
            "INSERT INTO diari (owner_id, nome, copertina) VALUES (%s, %s, %s) RETURNING *",
            (user_id, _nome(nome), _copertina(copertina))).fetchone())


def modifica_diario(user_id: UUID, diario_id: UUID, nome: str | None, copertina: str | None) -> dict:
    with user_tx(user_id) as conn:
        row = conn.execute(
            "UPDATE diari SET nome = coalesce(%s, nome), copertina = coalesce(%s, copertina) WHERE id = %s RETURNING *",
            (_nome(nome) if nome is not None else None, _copertina(copertina) if copertina is not None else None,
             diario_id)).fetchone()
    if row is None:
        raise AccountError(404, "diario_inesistente", "Diario non trovato")
    return diario_out(row)


def apri_diario(user_id: UUID, session_id: UUID | None, diario_id: UUID | None) -> dict | None:
    """Apre il diario sulla sessione (None lo chiude: si torna allo scaffale)."""
    row = None
    with user_tx(user_id) as conn:
        if diario_id is not None:
            row = conn.execute("UPDATE diari SET aperto_il = now() WHERE id = %s RETURNING *", (diario_id,)).fetchone()
            if row is None:
                raise AccountError(404, "diario_inesistente", "Diario non trovato")
    if session_id is not None:
        with system_tx() as conn:
            conn.execute("UPDATE sessions SET diario_id = %s WHERE id = %s", (diario_id, session_id))
    return diario_out(row) if row else None


def elimina_diario(user_id: UUID, diario_id: UUID, conferma: str) -> list[Path]:
    """Elimina il diario con tutto ciò che contiene. Restituisce i file audio da cancellare.

    `conferma` deve essere il nome del diario: è irreversibile. L'ultimo diario non si
    elimina (lo scaffale non resta mai vuoto).
    """
    with user_tx(user_id) as conn:
        d = conn.execute("SELECT nome FROM diari WHERE id = %s FOR UPDATE", (diario_id,)).fetchone()
        if d is None:
            raise AccountError(404, "diario_inesistente", "Diario non trovato")
        if " ".join(conferma.split()).casefold() != d["nome"].casefold():
            raise AccountError(422, "conferma_errata", "Per eliminarlo scrivi il nome del diario esattamente")
        if conn.execute("SELECT count(*) AS n FROM diari").fetchone()["n"] <= 1:
            raise AccountError(409, "ultimo_diario", "È il tuo unico diario: creane un altro prima di eliminarlo")
    with user_tx(user_id, diario_id) as conn:
        occupato = conn.execute(
            "SELECT 1 FROM ingestion_jobs WHERE locked_at > now() - interval '2 minutes' "
            "AND status NOT IN ('archiviazione_completata', 'errore_definitivo', 'annullato') LIMIT 1").fetchone()
        if occupato:
            raise AccountError(409, "in_elaborazione", "Un vocale di questo diario è in elaborazione: riprova tra poco")
        paths = []
        for r in conn.execute("SELECT storage_path FROM recordings").fetchall():
            try:
                paths.append(storage.absolute_path(r["storage_path"]))
            except ValueError:
                pass
        uploads = [r["id"] for r in conn.execute("SELECT id FROM upload_sessions WHERE status = 'aperta'").fetchall()]
        # Tutto il contenuto segue il diario (ON DELETE CASCADE su diario_id).
        conn.execute("DELETE FROM diari WHERE id = %s", (diario_id,))
    for u in uploads:
        storage.discard_upload(u)
    return paths
