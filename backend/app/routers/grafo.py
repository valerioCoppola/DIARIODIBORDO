"""Mappa dell'archivio: l'albero dei temi con la loro scheda, note e cose da fare.

    GET  /api/v1/grafo                tutto ciò che serve per navigare, in una richiesta: il
                                      telefono passa da un tema all'altro senza aspettare il PC.
                                      Ogni tema ha la sua tinta, calcolata dalla forma dell'albero.
    GET  /api/v1/temi/{id}            la scheda di un tema: a che punto è, sotto-temi, cose da
                                      fare (anche dei sotto-temi), appunti nel tempo.
    POST /api/v1/temi/{id}/sposta     {"dentro": id | null}: lo mette dentro un altro tema (o in cima)
    POST /api/v1/temi/{id}/rinomina   {"nome": "…"}; con il nome di un tema allo stesso livello, li unisce
    POST /api/v1/temi/{id}/conferma   un tema provvisorio (nato da una collocazione incerta) è giusto
Gli spostamenti e i nomi decisi a mano restano: il riordino automatico non li tocca, e il
modello li rilegge come indicazioni quando colloca i vocali successivi.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..conoscenza import Rifiutato, conferma_tema, discendenti, percorso, rinomina_tema, sposta_tema, tinte
from ..db import user_tx
from ..security import CurrentUser, CurrentUserDep
from .archive import with_original_offset

router = APIRouter(prefix="/api/v1", tags=["grafo"])

_DATA = "coalesce(e.data_ora, e.created_at)"


def _data(r: dict, data: str = "data_ora", creato: str = "created_at", off: str = "data_ora_offset_minutes"):
    return with_original_offset(r[data] or r[creato], r[off] if r[data] else None)


def secondi(segmenti: list[dict] | None, pos: int | None) -> float | None:
    """Istante dell'audio in cui inizia il testo alla posizione data."""
    if pos is None or not segmenti:
        return None
    best = None
    for s in segmenti:
        if s["char_start"] <= pos:
            best = s["inizio"]
        else:
            break
    return best


def _segmenti(conn: psycopg.Connection, entry_ids: set[UUID]) -> dict[UUID, list[dict]]:
    if not entry_ids:
        return {}
    return {r["entry_id"]: r["segments"] for r in conn.execute(
        """SELECT DISTINCT ON (entry_id) entry_id, segments FROM transcriptions
            WHERE entry_id = ANY(%s) ORDER BY entry_id, created_at DESC""", (list(entry_ids),)).fetchall()}


def leggi_appunti(conn: psycopg.Connection, entry_ids: list[UUID], topic_ids: list[UUID] | None = None,
                  segs: dict[UUID, list[dict]] | None = None) -> dict[UUID, list[dict]]:
    """Gli appunti di alcune note (solo di alcuni temi, se dati): per nota, blocchi con punti e dettagli."""
    if not entry_ids:
        return {}
    q = """SELECT p.id, p.entry_id, p.topic_id, p.tipo, p.testo, p.stato, p.char_start, p.parent_id, p.blocco,
                  p.action_item_id, a.stato AS a_stato, a.scadenza AS a_quando,
                  (SELECT count(*) FROM action_item_events ev WHERE ev.action_item_id = a.id
                     AND ev.event = 'ribadita') AS a_ribadita,
                  (SELECT o.id || '|' || coalesce(o.titolo, '') FROM action_item_events ev
                     JOIN entries o ON o.id = ev.source_entry_id
                    WHERE ev.action_item_id = a.id AND ev.origin = 'estratto_automatico'
                      AND ev.event IN ('completata', 'annullata') AND a.stato <> 'aperta'
                    ORDER BY ev.created_at DESC LIMIT 1) AS a_chiusa_da
             FROM punti p LEFT JOIN action_items a ON a.id = p.action_item_id
            WHERE p.entry_id = ANY(%s)"""
    params: list = [list(entry_ids)]
    if topic_ids is not None:
        q += " AND p.topic_id = ANY(%s)"
        params.append(list(topic_ids))
    rows = conn.execute(q + " ORDER BY p.entry_id, p.ord", params).fetchall()
    segs = segs if segs is not None else _segmenti(conn, {r["entry_id"] for r in rows})
    temi = {r["id"]: r for r in conn.execute(
        "SELECT t.id, t.label FROM topics t WHERE t.id = ANY(%s)",
        (list({r["topic_id"] for r in rows if r["topic_id"]}),)).fetchall()}
    colori = tinte(conn) if temi else {}

    def voce(r: dict) -> dict:
        az = None
        if r["action_item_id"]:
            chiusa = r["a_chiusa_da"].split("|", 1) if r["a_chiusa_da"] else None
            az = {"id": r["action_item_id"], "stato": r["a_stato"], "quando": r["a_quando"],
                  "ribadita": r["a_ribadita"], "chiusa_da": {"id": chiusa[0], "titolo": chiusa[1]} if chiusa else None}
        return {"id": r["id"], "tipo": r["tipo"], "testo": r["testo"], "stato": r["stato"],
                "secondi": secondi(segs.get(r["entry_id"]), r["char_start"]), "azione": az, "dettagli": []}

    out: dict[UUID, list[dict]] = {}
    per_id: dict[UUID, dict] = {}
    for r in rows:
        blocchi = out.setdefault(r["entry_id"], [])
        v = voce(r)
        per_id[r["id"]] = v
        if r["parent_id"]:
            padre = per_id.get(r["parent_id"])
            if padre is not None:
                padre["dettagli"].append(v)
            continue
        b = next((x for x in blocchi if x["_tema"] == r["topic_id"] and (r["topic_id"] or x["_blocco"] == r["blocco"])),
                 None)
        if b is None:
            t = temi.get(r["topic_id"])
            b = {"_tema": r["topic_id"], "_blocco": r["blocco"],
                 "tema": {"id": t["id"], "nome": t["label"], "tinta": colori.get(t["id"])} if t else None,
                 "punti": []}
            blocchi.append(b)
        b["punti"].append(v)
    for blocchi in out.values():
        for b in blocchi:
            b.pop("_tema")
            b.pop("_blocco")
    return out


@router.get("/grafo")
def grafo(user: CurrentUser = CurrentUserDep) -> dict:
    with user_tx(user.id, user.diario_id) as conn:
        note = conn.execute(
            f"""SELECT e.id, e.titolo, e.categoria::text AS categoria, e.data_ora, e.data_ora_offset_minutes,
                      e.created_at, e.executive_summary[1] AS sintesi,
                      coalesce(array_agg(DISTINCT coalesce(t.merged_into, t.id))
                               FILTER (WHERE t.id IS NOT NULL), '{{}}') AS concetti
                 FROM entries e
                 LEFT JOIN entry_topics et ON et.entry_id = e.id
                 LEFT JOIN topics t ON t.id = et.topic_id
                WHERE e.deleted_at IS NULL AND e.titolo IS NOT NULL
                GROUP BY e.id
                ORDER BY {_DATA} DESC""").fetchall()
        concetti = conn.execute(
            """SELECT t.id, t.label, t.tipo, t.sintesi, t.sintesi_il, t.parent_id, t.provvisorio, t.fissato,
                      t.created_at FROM topics t
                WHERE t.merged_into IS NULL
                ORDER BY t.label""").fetchall()
        colori = tinte(conn)
        chiarire = conn.execute("SELECT count(*) AS n FROM chiarimenti c JOIN entries e ON e.id = c.entry_id "
                                "AND e.deleted_at IS NULL WHERE c.stato = 'aperta'").fetchone()["n"]
        azioni = conn.execute(
            """SELECT a.id, a.entry_id, a.descrizione, a.responsabile, a.scadenza, a.stato, a.topic_id,
                      (SELECT count(*) FROM action_item_events ev
                        WHERE ev.action_item_id = a.id AND ev.event = 'ribadita') AS ribadita
                 FROM action_items a JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL
                WHERE a.stato = 'aperta'
                   OR (a.stato = 'completata' AND a.updated_at > now() - interval '3 days')
                ORDER BY coalesce(e.data_ora, e.created_at) DESC, a.ord NULLS LAST""").fetchall()
    return {
        "concetti": [{"id": c["id"], "nome": c["label"], "tipo": c["tipo"], "sintesi": c["sintesi"],
                      "aggiornato": c["sintesi_il"], "padre": c["parent_id"], "tinta": colori.get(c["id"], 0),
                      "provvisorio": c["provvisorio"], "fissato": c["fissato"], "creato": c["created_at"]}
                     for c in concetti],
        "da_chiarire": chiarire,
        "note": [{"id": n["id"], "titolo": n["titolo"], "categoria": n["categoria"], "sintesi": n["sintesi"],
                  "data": _data(n), "concetti": list(n["concetti"])} for n in note],
        "azioni": [{"id": a["id"], "nota": a["entry_id"], "testo": a["descrizione"], "chi": a["responsabile"],
                    "quando": a["scadenza"], "stato": a["stato"], "tema": a["topic_id"],
                    "ribadita": a["ribadita"]} for a in azioni],
    }


@router.get("/temi/{topic_id}")
def tema(topic_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    """La scheda di un tema: a che punto è, sotto-temi, cose da fare (anche dei sotto-temi), appunti nel tempo."""
    with user_tx(user.id, user.diario_id) as conn:
        t = conn.execute(
            """SELECT t.id, t.label, t.tipo, t.sintesi, t.sintesi_il, t.provvisorio, t.fissato
                 FROM topics x JOIN topics t ON t.id = coalesce(x.merged_into, x.id) WHERE x.id = %s""",
            (topic_id,)).fetchone()
        if t is None:
            raise HTTPException(404, "Tema non trovato")
        tutti = discendenti(conn, t["id"])
        figli = conn.execute(
            """SELECT t.id, t.label, t.tipo, t.sintesi,
                      (SELECT count(*) FROM action_items a WHERE a.topic_id = t.id AND a.stato = 'aperta') AS aperte
                 FROM topics t WHERE t.parent_id = %s AND t.merged_into IS NULL ORDER BY t.label""",
            (t["id"],)).fetchall()
        note = conn.execute(
            f"""SELECT DISTINCT e.id AS entry_id, e.titolo, e.data_ora, e.data_ora_offset_minutes, e.created_at
                  FROM entry_topics et JOIN topics x ON x.id = et.topic_id
                  JOIN entries e ON e.id = et.entry_id AND e.deleted_at IS NULL
                 WHERE coalesce(x.merged_into, x.id) = %s""", (t["id"],)).fetchall()
        note = sorted(note, key=lambda r: (r["data_ora"] or r["created_at"]), reverse=True)
        appunti = leggi_appunti(conn, [n["entry_id"] for n in note], [t["id"]])
        azioni = conn.execute(
            f"""SELECT a.id, a.descrizione, a.responsabile, a.scadenza, a.stato, a.char_start, a.topic_id,
                       e.id AS entry_id, e.titolo, e.data_ora, e.data_ora_offset_minutes, e.created_at,
                       (SELECT count(*) FROM action_item_events ev
                         WHERE ev.action_item_id = a.id AND ev.event = 'ribadita') AS ribadita
                  FROM action_items a JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL
                 WHERE a.topic_id = ANY(%s) AND a.stato = 'aperta'
                 ORDER BY {_DATA} DESC, a.ord NULLS LAST""", (tutti,)).fetchall()
        segs = _segmenti(conn, {a["entry_id"] for a in azioni})
        cammino = percorso(conn, t["id"])
        nomi = {r["id"]: r["label"] for r in conn.execute("SELECT id, label FROM topics WHERE id = ANY(%s)",
                                                           (tutti,)).fetchall()}
        colori = tinte(conn)

    def nota(r: dict) -> dict:
        return {"id": r["entry_id"], "titolo": r["titolo"], "data": _data(r)}

    return {
        "id": t["id"], "nome": t["label"], "tipo": t["tipo"], "sintesi": t["sintesi"], "aggiornato": t["sintesi_il"],
        "tinta": colori.get(t["id"], 0), "provvisorio": t["provvisorio"], "fissato": t["fissato"],
        "percorso": cammino[:-1],
        "sotto": [{"id": f["id"], "nome": f["label"], "tipo": f["tipo"], "sintesi": f["sintesi"],
                   "aperte": f["aperte"]} for f in figli],
        "azioni": [{"id": a["id"], "testo": a["descrizione"], "chi": a["responsabile"], "quando": a["scadenza"],
                    "stato": a["stato"], "nota": nota(a), "ribadita": a["ribadita"],
                    "secondi": secondi(segs.get(a["entry_id"]), a["char_start"]),
                    "tema": {"id": a["topic_id"], "nome": nomi.get(a["topic_id"])} if a["topic_id"] != t["id"]
                    else None} for a in azioni],
        "appunti": [{"nota": nota(n), "punti": [p for b in appunti.get(n["entry_id"], []) for p in b["punti"]]}
                    for n in note if appunti.get(n["entry_id"])],
    }


class SpostaIn(BaseModel):
    dentro: UUID | None = None


class RinominaIn(BaseModel):
    nome: str = Field(min_length=1, max_length=60)


def _correggi(user: CurrentUser, fn) -> dict:
    try:
        with user_tx(user.id, user.diario_id) as conn:
            return fn(conn)
    except Rifiutato as e:
        raise HTTPException(422, str(e)) from e


@router.post("/temi/{topic_id}/sposta")
def sposta(topic_id: UUID, body: SpostaIn, user: CurrentUser = CurrentUserDep) -> dict:
    def fn(conn):
        sposta_tema(conn, user.id, topic_id, body.dentro)
        return {"id": topic_id, "dentro": body.dentro}
    return _correggi(user, fn)


@router.post("/temi/{topic_id}/rinomina")
def rinomina(topic_id: UUID, body: RinominaIn, user: CurrentUser = CurrentUserDep) -> dict:
    def fn(conn):
        nome = rinomina_tema(conn, user.id, topic_id, body.nome)
        vivo = conn.execute("SELECT coalesce(merged_into, id) AS id FROM topics WHERE id = %s", (topic_id,)).fetchone()
        if vivo is None:      # unito a un tema con lo stesso nome
            vivo = conn.execute("SELECT id FROM topics WHERE lower(label) = lower(%s) AND merged_into IS NULL "
                                "ORDER BY fissato DESC LIMIT 1", (nome,)).fetchone()
        return {"id": vivo["id"] if vivo else None, "nome": nome}
    return _correggi(user, fn)


@router.post("/temi/{topic_id}/conferma")
def conferma(topic_id: UUID, user: CurrentUser = CurrentUserDep) -> dict:
    def fn(conn):
        conferma_tema(conn, topic_id)
        return {"id": topic_id, "provvisorio": False}
    return _correggi(user, fn)
