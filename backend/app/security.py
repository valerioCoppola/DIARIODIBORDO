"""Autenticazione: password Argon2id, sessioni via cookie, token API, CSRF."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
import psycopg
from fastapi import Depends, HTTPException, Request, status

from .config import get_settings
from .db import system_tx

_hasher = PasswordHasher()  # Argon2id, parametri predefiniti della libreria

SESSION_PREFIX = "ddbs_"
API_TOKEN_PREFIX = "ddbt_"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Hash fittizio per uniformare i tempi quando l'utente non esiste.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    username: str
    via: str                 # "session" o "api_token"
    session_id: UUID | None = None
    csrf_token: str | None = None
    is_admin: bool = False
    cambia_password: bool = False
    diario_id: UUID | None = None   # diario in cui lavora la richiesta
    diario_nome: str | None = None
    diario_copertina: str | None = None


# ---------------------------------------------------------------------------
# Limitazione dei tentativi di accesso (in memoria, per processo)
# ---------------------------------------------------------------------------


class LoginThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _window(self) -> float:
        return get_settings().login_lockout_minutes * 60

    def is_locked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = [t for t in self._failures.get(key, []) if now - t < self._window()]
            self._failures[key] = attempts
            return len(attempts) >= get_settings().login_max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


login_throttle = LoginThrottle()


# ---------------------------------------------------------------------------
# Sessioni
# ---------------------------------------------------------------------------


def authenticate(username: str, password: str) -> dict | None:
    with system_tx() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, disabled FROM users WHERE username = %s",
            (username.strip().lower(),),
        ).fetchone()
    if row is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if row["disabled"] or not verify_password(row["password_hash"], password):
        return None
    if _hasher.check_needs_rehash(row["password_hash"]):
        with system_tx() as conn:
            conn.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hash_password(password), row["id"]),
            )
    return row


def create_session(user_id: UUID, user_agent: str | None) -> tuple[str, str, datetime]:
    token = new_token(SESSION_PREFIX)
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(days=get_settings().session_ttl_days)
    with system_tx() as conn:
        conn.execute(
            """INSERT INTO sessions (user_id, token_hash, csrf_token, user_agent, expires_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_id, token_digest(token), csrf, (user_agent or "")[:200], expires),
        )
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
    return token, csrf, expires


def revoke_session(session_id: UUID) -> None:
    with system_tx() as conn:
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE id = %s", (session_id,))


def create_api_token(user_id: UUID, name: str) -> str:
    token = new_token(API_TOKEN_PREFIX)
    with system_tx() as conn:
        conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash) VALUES (%s, %s, %s)",
            (user_id, name, token_digest(token)),
        )
    return token


def _diario(conn: psycopg.Connection, user_id: UUID, richiesto: str | None, della_sessione: UUID | None,
            ultimo: bool) -> dict | None:
    """Il diario della richiesta: quello indicato con X-Diario, altrimenti quello aperto
    sulla sessione; per i token API, se non indicato, l'ultimo aperto."""
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))   # RLS su diari
    q = "SELECT id, nome, copertina FROM diari"
    if richiesto:
        try:
            did = UUID(richiesto)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Diario non valido")
        row = conn.execute(q + " WHERE id = %s", (did,)).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                {"errore": "diario_inesistente", "messaggio": "Diario non trovato"})
        return row
    if della_sessione:
        return conn.execute(q + " WHERE id = %s", (della_sessione,)).fetchone()
    if ultimo:
        return conn.execute(q + " ORDER BY aperto_il DESC NULLS LAST, created_at LIMIT 1").fetchone()
    return None


def _current(row: dict, via: str, diario: dict | None, **kw: object) -> CurrentUser:
    return CurrentUser(row["id"], row["username"], via, is_admin=row["is_admin"],
                       cambia_password=row["cambia_password"],
                       diario_id=diario["id"] if diario else None,
                       diario_nome=diario["nome"] if diario else None,
                       diario_copertina=diario["copertina"] if diario else None, **kw)


def _user_from_session(token: str, richiesto: str | None) -> CurrentUser | None:
    with system_tx() as conn:
        row = conn.execute(
            """SELECT s.id AS session_id, s.csrf_token, s.diario_id, u.id, u.username, u.is_admin, u.cambia_password
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.revoked_at IS NULL
                  AND s.expires_at > now() AND NOT u.disabled""",
            (token_digest(token),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE sessions SET last_seen_at = now() WHERE id = %s "
            "AND last_seen_at < now() - interval '5 minutes'",
            (row["session_id"],),
        )
        diario = _diario(conn, row["id"], richiesto, row["diario_id"], ultimo=False)
    return _current(row, "session", diario, session_id=row["session_id"], csrf_token=row["csrf_token"])


def _user_from_api_token(token: str, richiesto: str | None) -> CurrentUser | None:
    with system_tx() as conn:
        row = conn.execute(
            """SELECT t.id AS token_id, u.id, u.username, u.is_admin, u.cambia_password
                 FROM api_tokens t JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = %s AND t.revoked_at IS NULL AND NOT u.disabled""",
            (token_digest(token),),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_tokens SET last_used_at = now() WHERE id = %s", (row["token_id"],))
        diario = _diario(conn, row["id"], richiesto, None, ultimo=True)
    return _current(row, "api_token", diario)


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Autenticazione richiesta")


def current_account(request: Request) -> CurrentUser:
    """Dipendenza FastAPI: richiede sessione valida o token API, anche senza un diario aperto.

    Con sessione via cookie, le richieste che modificano dati devono portare
    l'header X-CSRF-Token della sessione (oltre a SameSite=Strict sul cookie).
    """
    richiesto = request.headers.get("x-diario") or None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        user = _user_from_api_token(token, richiesto) if token.startswith(API_TOKEN_PREFIX) else None
        if user is None:
            raise _unauthorized()
        return user

    token = request.cookies.get(get_settings().session_cookie_name)
    if not token or not token.startswith(SESSION_PREFIX):
        raise _unauthorized()
    user = _user_from_session(token, richiesto)
    if user is None:
        raise _unauthorized()

    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, user.csrf_token or ""):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Token CSRF mancante o non valido")
    return user


def current_user(user: CurrentUser = Depends(current_account)) -> CurrentUser:
    """Dipendenza FastAPI per i contenuti: come current_account, dentro un diario aperto."""
    if user.cambia_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {
            "errore": "cambia_password", "messaggio": "Prima di continuare scegli una nuova password"})
    if user.diario_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "errore": "nessun_diario", "messaggio": "Nessun diario aperto: scegline uno"})
    return user


def admin_user(user: CurrentUser = Depends(current_account)) -> CurrentUser:
    if not user.is_admin or user.cambia_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo l'amministratore")
    return user


CurrentUserDep = Depends(current_user)
AccountDep = Depends(current_account)
AdminDep = Depends(admin_user)
