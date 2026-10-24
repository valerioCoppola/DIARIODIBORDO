"""Gestione degli account, solo per l'amministratore: persone e inviti.

Nessun accesso ai contenuti: l'amministratore vede nomi utente e date, mai i diari.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import account
from ..account import AccountError
from ..security import AdminDep, CurrentUser

router = APIRouter(prefix="/api/v1/admin", tags=["amministrazione"])


class InvitoIn(BaseModel):
    nota: str | None = Field(default=None, max_length=80)
    giorni: int = Field(default=account.INVITO_GIORNI, ge=1, le=60)


def _err(e: AccountError) -> HTTPException:
    return HTTPException(e.status, {"errore": e.code, "messaggio": e.message})


@router.get("/utenti")
def utenti(admin: CurrentUser = AdminDep) -> dict:
    return {"utenti": [
        {"id": u["id"], "username": u["username"], "amministratore": u["is_admin"], "attivo": not u["disabled"],
         "password_provvisoria": u["cambia_password"], "creato_il": u["created_at"],
         "ultimo_accesso": u["last_login_at"], "visto_il": u["visto_il"], "sei_tu": u["id"] == admin.id}
        for u in account.elenco_utenti()]}


@router.post("/utenti/{user_id}/attiva")
def attiva(user_id: UUID, admin: CurrentUser = AdminDep) -> dict:
    try:
        account.imposta_attivo(admin.id, user_id, True)
    except AccountError as e:
        raise _err(e)
    return {"ok": True}


@router.post("/utenti/{user_id}/disattiva")
def disattiva(user_id: UUID, admin: CurrentUser = AdminDep) -> dict:
    try:
        account.imposta_attivo(admin.id, user_id, False)
    except AccountError as e:
        raise _err(e)
    return {"ok": True}


@router.post("/utenti/{user_id}/password")
def password(user_id: UUID, admin: CurrentUser = AdminDep) -> dict:
    """Password provvisoria (mostrata solo ora): al primo accesso la persona ne sceglie una sua."""
    try:
        return {"password": account.reimposta_password(admin.id, user_id)}
    except AccountError as e:
        raise _err(e)


@router.get("/inviti")
def inviti(admin: CurrentUser = AdminDep) -> dict:
    out, ora = [], datetime.now(timezone.utc)
    for i in account.elenco_inviti():
        stato = ("usato" if i["usato_il"] else "revocato" if i["revocato_il"] else
                 "scaduto" if i["scade_il"] < ora else "valido")
        out.append({"id": i["id"], "nota": i["nota"], "creato_il": i["created_at"], "scade_il": i["scade_il"],
                    "usato_il": i["usato_il"], "usato_da": i["usato_da"], "stato": stato})
    return {"inviti": out}


@router.post("/inviti", status_code=201)
def nuovo_invito(body: InvitoIn, admin: CurrentUser = AdminDep) -> dict:
    codice, scade = account.crea_invito(admin.id, body.nota, body.giorni)
    return {"codice": codice, "scade_il": scade}


@router.delete("/inviti/{invito_id}")
def revoca(invito_id: UUID, admin: CurrentUser = AdminDep) -> dict:
    account.revoca_invito(invito_id)
    return {"ok": True}
