from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import account
from ..account import AccountError
from ..config import get_settings
from ..db import system_tx
from ..security import (
    AccountDep,
    CurrentUser,
    authenticate,
    create_session,
    login_throttle,
    revoke_session,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["autenticazione"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class RegistratiIn(BaseModel):
    codice: str = Field(min_length=4, max_length=40)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordIn(BaseModel):
    attuale: str = Field(min_length=1, max_length=1024)
    nuova: str = Field(min_length=1, max_length=1024)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _apri_sessione(user_id, username: str, request: Request, response: Response) -> dict:
    token, csrf, expires = create_session(user_id, request.headers.get("user-agent"))
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name, token,
        max_age=s.session_ttl_days * 86400, expires=expires,
        httponly=True, secure=s.cookie_secure, samesite="strict", path="/",
    )
    return {"username": username, "csrf_token": csrf, "scade_il": expires}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    key_user = f"u:{body.username.strip().lower()}"
    key_ip = f"ip:{_client_ip(request)}"
    if login_throttle.is_locked(key_user) or login_throttle.is_locked(key_ip):
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return {"errore": "troppi_tentativi", "messaggio": "Troppi tentativi falliti, riprova più tardi"}

    user = authenticate(body.username, body.password)
    if user is None:
        login_throttle.fail(key_user)
        login_throttle.fail(key_ip)
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"errore": "credenziali_non_valide", "messaggio": "Nome utente o password non validi"}

    login_throttle.reset(key_user)
    return _apri_sessione(user["id"], user["username"], request, response)


@router.post("/registrati")
def registrati(body: RegistratiIn, request: Request, response: Response) -> dict:
    """Nuovo account con un codice d'invito dell'amministratore; si entra subito."""
    key_ip = f"ip:{_client_ip(request)}"
    if login_throttle.is_locked(key_ip):
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return {"errore": "troppi_tentativi", "messaggio": "Troppi tentativi falliti, riprova più tardi"}
    try:
        uid = account.registra(body.codice, body.username, body.password)
    except AccountError as e:
        if e.code == "invito_non_valido":
            login_throttle.fail(key_ip)
        response.status_code = e.status
        return {"errore": e.code, "messaggio": e.message}
    return _apri_sessione(uid, account.normalizza_username(body.username), request, response)


@router.post("/password")
def cambia_password(body: PasswordIn, request: Request, user: CurrentUser = AccountDep) -> dict:
    """La persona cambia la propria password; le sue altre sessioni si chiudono."""
    key_user = f"u:{user.username}"
    if login_throttle.is_locked(key_user):
        raise HTTPException(429, {"errore": "troppi_tentativi", "messaggio": "Troppi tentativi falliti, riprova più tardi"})
    with system_tx() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user.id,)).fetchone()
        if not verify_password(row["password_hash"], body.attuale):
            login_throttle.fail(key_user)
            raise HTTPException(403, {"errore": "password_errata", "messaggio": "La password attuale non è giusta"})
        if body.nuova == body.attuale:
            raise HTTPException(422, {"errore": "password_uguale", "messaggio": "Scegline una diversa da quella attuale"})
        try:
            account.imposta_password(conn, user.id, body.nuova, provvisoria=False, tranne_sessione=user.session_id)
        except AccountError as e:
            raise HTTPException(e.status, {"errore": e.code, "messaggio": e.message})
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, user: CurrentUser = AccountDep) -> dict:
    if user.session_id:
        revoke_session(user.session_id)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: CurrentUser = AccountDep) -> dict:
    diario = ({"id": user.diario_id, "nome": user.diario_nome, "copertina": user.diario_copertina}
              if user.diario_id else None)
    return {"id": user.id, "username": user.username, "autenticazione": user.via, "csrf_token": user.csrf_token,
            "amministratore": user.is_admin, "cambia_password": user.cambia_password, "diario": diario}
