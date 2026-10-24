"""Domande del sistema e indicazioni della persona.

    GET    /api/v1/chiarimenti          domande aperte: dove va un argomento, se un nome è un tema già noto
    POST   /api/v1/chiarimenti/{id}     {"scelta": n} una delle opzioni, oppure {"testo": "…"} a parole
    GET    /api/v1/indicazioni          ciò che la persona ha detto rispondendo o correggendo
    DELETE /api/v1/indicazioni/{id}     un'indicazione sbagliata non vale più

Una scelta si applica subito (la nota passa nel tema giusto, e il percorso resta confermato
anche se l'albero si ricostruisce); poi il worker aggiorna le schede dei temi toccati. Una
risposta a parole diventa un'indicazione e la nota viene collocata di nuovo dal modello.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..coda import rimetti_in_coda
from ..conoscenza import Rifiutato, rispondi
from ..db import user_tx
from ..security import CurrentUser, CurrentUserDep
from .archive import with_original_offset

router = APIRouter(prefix="/api/v1", tags=["chiarimenti"])


def domande(conn: psycopg.Connection, entry_id: UUID | None = None, limite: int = 50) -> list[dict]:
    q = """SELECT c.id, c.entry_id, c.blocco, c.tipo, c.domanda, c.opzioni, c.created_at, b.argomento,
                  e.titolo, e.data_ora, e.data_ora_offset_minutes, e.created_at AS e_creata,
                  (SELECT array_agg(p.testo ORDER BY p.ord) FROM (
                      SELECT testo, ord FROM punti WHERE entry_id = c.entry_id AND blocco = c.blocco
                         AND parent_id IS NULL ORDER BY ord LIMIT 3) p) AS punti
             FROM chiarimenti c JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL
             LEFT JOIN blocchi b ON b.entry_id = c.entry_id AND b.ord = c.blocco
            WHERE c.stato = 'aperta'"""
    params: list = []
    if entry_id is not None:
        q += " AND c.entry_id = %s"
        params.append(entry_id)
    rows = conn.execute(q + " ORDER BY c.created_at DESC, c.blocco LIMIT %s", (*params, limite)).fetchall()
    return [{
        "id": r["id"], "tipo": r["tipo"], "domanda": r["domanda"], "argomento": r["argomento"],
        "punti": list(r["punti"] or []),
        "opzioni": [{"n": i, "etichetta": o["etichetta"]} for i, o in enumerate(r["opzioni"] or [])],
        "nota": {"id": r["entry_id"], "titolo": r["titolo"],
                 "data": with_original_offset(r["data_ora"] or r["e_creata"],
                                              r["data_ora_offset_minutes"] if r["data_ora"] else None)},
    } for r in rows]


@router.get("/chiarimenti")
def elenco(user: CurrentUser = CurrentUserDep) -> list[dict]:
    with user_tx(user.id, user.diario_id) as conn:
        return domande(conn)


class RispostaIn(BaseModel):
    scelta: int | None = Field(default=None, ge=0, le=10)
    testo: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _una(self):
        if (self.scelta is None) == (not (self.testo or "").strip()):
            raise ValueError("indica una scelta oppure una risposta a parole")
        return self


@router.post("/chiarimenti/{chiarimento_id}")
def risposta(chiarimento_id: UUID, body: RispostaIn, user: CurrentUser = CurrentUserDep) -> dict:
    try:
        with user_tx(user.id, user.diario_id) as conn:
            entry = conn.execute("SELECT entry_id FROM chiarimenti WHERE id = %s", (chiarimento_id,)).fetchone()
            if entry is None:
                raise HTTPException(404, "Domanda non trovata")
            modo = rispondi(conn, user.id, chiarimento_id, body.scelta, body.testo if body.scelta is None else None)
            msg = ("risposta a una domanda: schede dei temi da aggiornare" if modo == "evolvi"
                   else "risposta a parole: nuova collocazione")
            in_coda = rimetti_in_coda(conn, user.id, entry["entry_id"], modo, msg)
    except Rifiutato as e:
        raise HTTPException(422, str(e)) from e
    return {"id": chiarimento_id, "applicata": modo == "evolvi", "ricolloca": modo == "colloca", "in_coda": in_coda}


@router.get("/indicazioni")
def indicazioni(user: CurrentUser = CurrentUserDep) -> list[dict]:
    with user_tx(user.id, user.diario_id) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, testo, entry_id AS nota, created_at AS data FROM indicazioni ORDER BY created_at DESC "
            "LIMIT 200").fetchall()]


@router.delete("/indicazioni/{indicazione_id}")
def togli(indicazione_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        n = conn.execute("DELETE FROM indicazioni WHERE id = %s", (indicazione_id,)).rowcount
    if not n:
        raise HTTPException(404, "Indicazione non trovata")
    return {"id": indicazione_id, "eliminata": True}
