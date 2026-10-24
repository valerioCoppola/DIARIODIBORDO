"""Domande sull'archivio: risposta del modello in streaming, con le fonti prima del testo."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..chiedi import contesto_token, messaggi, raccogli
from ..config import get_settings
from ..conoscenza import tinte
from ..db import user_tx
from ..pipeline.errors import PipelineError
from ..pipeline.ollama import OllamaClient
from ..security import CurrentUser, CurrentUserDep

log = logging.getLogger("diario.chiedi")
router = APIRouter(prefix="/api/v1", tags=["chiedi"])
_client: OllamaClient | None = None


def _ollama() -> OllamaClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = OllamaClient(s.ollama_url, timeout=300.0, keep_alive=s.ollama_keep_alive)
    return _client


def set_client(c: OllamaClient | None) -> None:
    """Per i test."""
    global _client
    _client = c


class Domanda(BaseModel):
    domanda: str = Field(min_length=2, max_length=500)
    oggi: datetime | None = Field(default=None, description="ora del telefono, con il fuso: per 'ieri', 'lunedì'…")
    ramo: UUID | None = None          # solo le note di un tema e dei suoi sotto-temi
    dal: datetime | None = None


def _riga(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode()


@router.post("/chiedi")
def chiedi(body: Domanda, user: CurrentUser = CurrentUserDep) -> StreamingResponse:
    """NDJSON: {"fonti": [...], "periodo": ...}, poi {"t": "pezzo di risposta"}…, infine {"fine": true}.

    Se non c'è niente di pertinente: {"fonti": []} e una risposta fissa, senza chiamare il modello.
    In caso di problemi del modello: {"errore": "..."} (le fonti restano utili).
    """
    ora = body.oggi or datetime.now(timezone.utc).astimezone()
    with user_tx(user.id, user.diario_id) as conn:
        ctx = raccogli(conn, body.domanda, ora, body.ramo, body.dal)
    testa = {"fonti": [f.json() for f in ctx.fonti], "periodo": ctx.periodo.nome if ctx.periodo else None}

    def flusso():
        yield _riga(testa)
        if ctx.vuoto():
            dove = f" per {ctx.periodo.nome}" if ctx.periodo else ""
            yield _riga({"t": f"Nel diario non trovo niente su questo{dove}."})
            yield _riga({"fine": True})
            return
        s = get_settings()
        msgs = messaggi(body.domanda, ctx, ora)
        try:
            for pezzo in _ollama().chat_stream(s.ollama_model, msgs, contesto_token(msgs)):
                yield _riga({"t": pezzo})
        except PipelineError as e:
            log.warning("domanda senza risposta: %s", e)
            yield _riga({"errore": "Il motore di analisi non risponde: le fonti qui sotto restano valide."})
            return
        yield _riga({"fine": True})

    return StreamingResponse(flusso(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/rami")
def rami(user: CurrentUser = CurrentUserDep) -> list[dict]:
    """I temi più ampi (in cima all'albero), per restringere ricerca e domande a un ramo."""
    with user_tx(user.id, user.diario_id) as conn:
        colori = tinte(conn)
        return [{"id": r["id"], "nome": r["label"], "tinta": colori.get(r["id"], 0)} for r in conn.execute(
            "SELECT id, label FROM topics WHERE parent_id IS NULL AND merged_into IS NULL ORDER BY created_at"
        ).fetchall()]
