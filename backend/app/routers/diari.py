"""I diari della persona: scaffale, nuovo, rinomina, apri, chiudi, elimina."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import account
from ..account import AccountError
from ..playback import remove_files
from ..security import AccountDep, CurrentUser

router = APIRouter(prefix="/api/v1/diari", tags=["diari"])

Copertina = Literal["cuoio", "notte", "bosco", "vino", "ardesia", "senape"]


class DiarioIn(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    copertina: Copertina | None = None


class DiarioPatch(BaseModel):
    nome: str | None = Field(default=None, min_length=1, max_length=80)
    copertina: Copertina | None = None


class Conferma(BaseModel):
    conferma: str = Field(max_length=80)


def _err(e: AccountError) -> HTTPException:
    return HTTPException(e.status, {"errore": e.code, "messaggio": e.message})


def _attivo(user: CurrentUser) -> None:
    if user.cambia_password:
        raise HTTPException(403, {"errore": "cambia_password", "messaggio": "Prima di continuare scegli una nuova password"})


@router.get("")
def elenco(user: CurrentUser = AccountDep) -> dict:
    _attivo(user)
    return {"diari": account.elenco_diari(user.id), "aperto": user.diario_id}


@router.post("", status_code=201)
def nuovo(body: DiarioIn, user: CurrentUser = AccountDep) -> dict:
    _attivo(user)
    try:
        return account.crea_diario(user.id, body.nome, body.copertina)
    except AccountError as e:
        raise _err(e)


@router.patch("/{diario_id}")
def modifica(diario_id: UUID, body: DiarioPatch, user: CurrentUser = AccountDep) -> dict:
    _attivo(user)
    try:
        return account.modifica_diario(user.id, diario_id, body.nome, body.copertina)
    except AccountError as e:
        raise _err(e)


@router.post("/{diario_id}/apri")
def apri(diario_id: UUID, user: CurrentUser = AccountDep) -> dict:
    _attivo(user)
    try:
        return account.apri_diario(user.id, user.session_id, diario_id)
    except AccountError as e:
        raise _err(e)


@router.post("/chiudi")
def chiudi(user: CurrentUser = AccountDep) -> dict:
    account.apri_diario(user.id, user.session_id, None)
    return {"ok": True}


@router.post("/{diario_id}/elimina")
def elimina(diario_id: UUID, body: Conferma, user: CurrentUser = AccountDep) -> dict:
    _attivo(user)
    try:
        paths = account.elimina_diario(user.id, diario_id, body.conferma)
    except AccountError as e:
        raise _err(e)
    for path in paths:
        remove_files(path)
    return {"ok": True, "file_eliminati": len(paths)}
