"""Ricerca nell'archivio: testuale (dizionario italiano) e per significato (embedding).

I risultati delle diverse liste (segmenti per parole, note per titolo e testo,
segmenti per vicinanza di significato) sono fusi con la Reciprocal Rank Fusion:
conta la posizione in ciascuna lista, non il punteggio grezzo, quindi le due
ricerche si combinano senza tarature. Se Ollama non risponde, la ricerca resta
solo testuale e lo dice.

Oltre alle note, la ricerca restituisce ciò che l'analisi ha già estratto:
argomenti (con il ramo dell'albero a cui appartengono) e cose da fare. Le note di un
argomento riconosciuto con sicurezza entrano nella fusione come una lista in più. Si
può restringere a un ramo (un tema e i suoi sotto-temi) e a un periodo. Tutto in Postgres: pochi millisecondi (più l'embedding
della domanda).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable
from uuid import UUID

import psycopg

from .config import get_settings
from .pipeline.errors import PipelineError
from .pipeline.ollama import OllamaClient

log = logging.getLogger("diario.ricerca")

_RAMO = """(WITH RECURSIVE giu AS (SELECT %(ramo)s::uuid AS id UNION SELECT t.id FROM topics t JOIN giu ON t.parent_id = giu.id)
            SELECT id FROM giu)"""


def _tinte(conn):
    from .conoscenza import tinte

    return tinte(conn)

RRF_K = 60
CANDIDATES = 100
HL_START, HL_STOP = "\u0001", "\u0002"   # marcatori dell'evidenziazione, resi dal frontend

QueryEmbedder = Callable[[str], list[float]]
_override: QueryEmbedder | None = None
_client: OllamaClient | None = None


def set_query_embedder(fn: QueryEmbedder | None) -> None:
    """Per i test: sostituisce il calcolo dell'embedding della domanda."""
    global _override
    _override = fn


def embed_query(q: str) -> list[float] | None:
    if _override is not None:
        return _override(q)
    global _client
    s = get_settings()
    if _client is None:
        _client = OllamaClient(s.ollama_url, timeout=20.0, keep_alive=s.ollama_keep_alive, embed_cpu=s.embedding_su_cpu)
    try:
        return _client.embed(s.ollama_embed_model, [q]).vectors[0]
    except PipelineError as e:
        log.info("ricerca solo testuale: %s", e)
        return None


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def search(conn: psycopg.Connection, q: str, limit: int = 20, ramo: UUID | None = None,
           dal: datetime | None = None) -> dict:
    from .chiedi import _filtro

    s = get_settings()
    filtro = _filtro(ramo, dal, None)
    cose = find_things(conn, q, filtro, ramo, dal)
    emb = embed_query(q)
    params: dict = {"q": q, "n": CANDIDATES, "k": RRF_K, "limit": limit, "ramo": ramo, "dal": dal,
                    "tids": cose.pop("_tids")}
    vec_cte = ""
    vec_union = ""
    lnk_cte = ""
    lnk_union = ""
    if params["tids"]:
        lnk_cte = f""",
        lnk AS (
          SELECT entry_id, row_number() OVER (ORDER BY d DESC) AS r FROM (
            SELECT e.id AS entry_id, coalesce(e.data_ora, e.created_at) AS d FROM entries e
             WHERE e.deleted_at IS NULL {filtro} AND
               EXISTS (SELECT 1 FROM entry_topics et JOIN topics t ON t.id = et.topic_id
                        WHERE et.entry_id = e.id AND coalesce(t.merged_into, t.id) = ANY(%(tids)s::uuid[]))
             ORDER BY d DESC LIMIT %(n)s) x)"""
        lnk_union = "UNION ALL SELECT entry_id, 1.0 / (%(k)s + r) FROM lnk"
    if emb is not None:
        params["emb"] = _vec(emb)
        params["maxd"] = s.search_max_distance
        vec_cte = f""",
        vec AS (
          SELECT entry_id, id AS chunk_id, d, row_number() OVER (ORDER BY d) AS r FROM (
            SELECT c.entry_id, c.id, c.embedding <=> %(emb)s::vector AS d
              FROM text_chunks c JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL
             WHERE c.embedding IS NOT NULL {filtro} ORDER BY c.embedding <=> %(emb)s::vector LIMIT %(n)s) x
          WHERE d <= %(maxd)s)"""
        vec_union = "UNION ALL SELECT entry_id, 1.0 / (%(k)s + min(r)) FROM vec GROUP BY entry_id"

    sql = f"""
    WITH tsq AS (SELECT websearch_to_tsquery('italian', %(q)s) AS q),
    fts_c AS (
      SELECT entry_id, chunk_id, row_number() OVER (ORDER BY score DESC) AS r FROM (
        SELECT c.entry_id, c.id AS chunk_id, ts_rank_cd(c.search_tsv, tsq.q) AS score
          FROM text_chunks c JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL, tsq
         WHERE c.search_tsv @@ tsq.q {filtro} ORDER BY score DESC LIMIT %(n)s) x),
    fts_e AS (
      SELECT entry_id, row_number() OVER (ORDER BY score DESC) AS r FROM (
        SELECT e.id AS entry_id, ts_rank_cd(e.search_tsv, tsq.q) AS score
          FROM entries e, tsq WHERE e.deleted_at IS NULL AND e.search_tsv @@ tsq.q {filtro}
         ORDER BY score DESC LIMIT %(n)s) x){vec_cte}{lnk_cte},
    fused AS (
      SELECT entry_id, sum(s) AS score FROM (
        SELECT entry_id, 1.0 / (%(k)s + min(r)) AS s FROM fts_c GROUP BY entry_id
        UNION ALL SELECT entry_id, 1.0 / (%(k)s + min(r)) FROM fts_e GROUP BY entry_id
        {vec_union}
        {lnk_union}) u
      GROUP BY entry_id)
    SELECT f.entry_id, f.score,
           (SELECT chunk_id FROM fts_c WHERE fts_c.entry_id = f.entry_id ORDER BY r LIMIT 1) AS fts_chunk,
           {"(SELECT chunk_id FROM vec WHERE vec.entry_id = f.entry_id ORDER BY r LIMIT 1)" if emb is not None else "NULL::uuid"} AS vec_chunk
      FROM fused f JOIN entries e ON e.id = f.entry_id AND e.deleted_at IS NULL
     ORDER BY f.score DESC, coalesce(e.data_ora, e.created_at) DESC
     LIMIT %(limit)s"""
    rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        results.append(_result(conn, r["entry_id"], float(r["score"]), r["fts_chunk"], r["vec_chunk"], q))
    return {"modalita": "ibrida" if emb is not None else "testuale", "risultati": results, **cose}


def _result(conn: psycopg.Connection, entry_id: UUID, score: float, fts_chunk: UUID | None,
            vec_chunk: UUID | None, q: str) -> dict:
    e = conn.execute("SELECT id, titolo, data_ora, data_ora_offset_minutes, categoria::text AS categoria, "
                     "executive_summary, created_at FROM entries WHERE id = %s", (entry_id,)).fetchone()
    snippet = None
    char_start = None
    chunk_id = fts_chunk or vec_chunk
    if chunk_id is not None:
        c = conn.execute(
            f"""SELECT char_start,
                       CASE WHEN %(fts)s THEN ts_headline('italian', content, websearch_to_tsquery('italian', %(q)s),
                            'StartSel={HL_START}, StopSel={HL_STOP}, MaxWords=40, MinWords=18, MaxFragments=2, FragmentDelimiter= … ')
                            ELSE left(content, 320) END AS snippet
                  FROM text_chunks WHERE id = %(id)s""",
            {"fts": fts_chunk is not None, "q": q, "id": chunk_id}).fetchone()
        snippet, char_start = c["snippet"], c["char_start"]
    from .routers.archive import with_original_offset

    rami = conn.execute(
        """WITH RECURSIVE su AS (
               SELECT coalesce(x.merged_into, x.id) AS id, 0 AS d FROM entry_topics et
                 JOIN topics x ON x.id = et.topic_id WHERE et.entry_id = %s
               UNION
               SELECT t.parent_id, su.d + 1 FROM topics t JOIN su ON t.id = su.id
                WHERE t.parent_id IS NOT NULL AND su.d < 12)
           SELECT DISTINCT t.id, t.label FROM su JOIN topics t ON t.id = su.id WHERE t.parent_id IS NULL
            ORDER BY t.label""", (entry_id,)).fetchall()
    colori = _tinte(conn) if rami else {}
    return {
        "entry_id": e["id"], "titolo": e["titolo"], "categoria": e["categoria"],
        "data_ora": with_original_offset(e["data_ora"], e["data_ora_offset_minutes"]),
        "creato_il": e["created_at"], "executive_summary": e["executive_summary"],
        "frammento": snippet, "secondi": seconds_at(conn, entry_id, char_start),
        "punteggio": round(score, 5), "per_significato": fts_chunk is None and vec_chunk is not None,
        "rami": [{"nome": a["label"], "tinta": colori.get(a["id"], 0)} for a in rami],
    }


def seconds_at(conn: psycopg.Connection, entry_id: UUID, char_pos: int | None) -> float | None:
    """Istante dell'audio in cui inizia il testo alla posizione data."""
    if char_pos is None:
        return None
    row = conn.execute("SELECT segments FROM transcriptions WHERE entry_id = %s ORDER BY created_at DESC LIMIT 1",
                       (entry_id,)).fetchone()
    if not row:
        return None
    best = None
    for seg in row["segments"]:
        if seg["char_start"] <= char_pos:
            best = seg["inizio"]
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Argomenti e cose da fare
# ---------------------------------------------------------------------------

_DA, _A = "àáâäãèéêëìíîïòóôöõùúûüç", "aaaaaeeeeiiiiooooouuuuc"
_TAB = str.maketrans(_DA, _A)


def _norm(t: str) -> str:
    return " ".join(t.lower().translate(_TAB).split())


def _n(expr: str) -> str:
    """Minuscolo e senza accenti, come _norm (lato SQL)."""
    return f"translate(lower({expr}), '{_DA}', '{_A}')"


def _like(t: str) -> str:
    return t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _punteggio(nome: str, alias: str) -> str:
    """Quanto un nome corrisponde alla ricerca: uguale, inizia così, la contiene,
    oppure è contenuto nella frase cercata ("riunione con Marco")."""
    return f"""(CASE WHEN {nome} = %(nq)s THEN 8 ELSE 0 END
              + CASE WHEN {nome} LIKE %(pre)s THEN 4 ELSE 0 END
              + CASE WHEN length({nome}) >= 3 AND strpos(%(nq)s, {nome}) > 0 THEN 3 ELSE 0 END
              + CASE WHEN {nome} LIKE %(sub)s OR {alias} LIKE %(sub)s THEN 2 ELSE 0 END)"""


def find_things(conn: psycopg.Connection, q: str, filtro: str = "", ramo: UUID | None = None,
                dal: datetime | None = None) -> dict:
    vuoto = {"concetti": [], "azioni": [], "_tids": []}
    nq = _norm(q)
    parole = [w for w in re.findall(r"\w+", nq) if len(w) >= 2]
    if not parole:
        return vuoto
    params = {"q": q, "nq": nq, "orq": " or ".join(parole), "pre": _like(nq) + "%",
              "sub": f"%{_like(nq)}%" if len(nq) >= 3 else None, "ramo": ramo, "dal": dal}
    # Corrispondenza sicura: abbastanza per collegare le note dell'argomento.
    forte = "(p >= 8 OR (p >= 2 AND length(%(nq)s) >= 3))"

    vt = "to_tsvector('italian', t.label || ' ' || array_to_string(t.aliases, ' '))"
    concetti = conn.execute(f"""
        WITH tq AS (SELECT websearch_to_tsquery('italian', %(orq)s) AS q)
        SELECT id, nome, note, {forte} AS forte FROM (
          SELECT t.id, t.label AS nome,
                 {_punteggio(_n("t.label"), _n("array_to_string(t.aliases, ' ')"))} AS p,
                 {vt} @@ tq.q AS fts, ts_rank({vt}, tq.q) AS r,
                 (SELECT count(DISTINCT et.entry_id) FROM entry_topics et JOIN topics x ON x.id = et.topic_id
                    JOIN entries e ON e.id = et.entry_id AND e.deleted_at IS NULL
                   WHERE coalesce(x.merged_into, x.id) = t.id) AS note
            FROM topics t, tq
           WHERE t.merged_into IS NULL {"AND t.id IN " + _RAMO if ramo else ""}) c
         WHERE (p > 0 OR fts) AND note > 0
         ORDER BY p DESC, r DESC, note DESC LIMIT 8""", params).fetchall()

    colori = _tinte(conn) if concetti else {}
    radici: dict = {}
    for c in concetti:
        from .conoscenza import percorso

        cam = percorso(conn, c["id"])
        radici[c["id"]] = cam[0]["nome"] if len(cam) > 1 else None

    va = "to_tsvector('italian', a.descrizione || ' ' || coalesce(a.responsabile, ''))"
    azioni = conn.execute(f"""
        WITH tq AS (SELECT websearch_to_tsquery('italian', %(q)s) AS q)
        SELECT a.id, a.descrizione, a.responsabile, a.scadenza, a.stato, e.id AS nota, e.titolo
          FROM action_items a JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL, tq
         WHERE a.stato <> 'annullata' {filtro}
           AND ({va} @@ tq.q OR {_n("coalesce(a.responsabile, '')")} LIKE %(sub)s)
         ORDER BY (a.stato = 'aperta') DESC, coalesce(e.data_ora, e.created_at) DESC
         LIMIT 8""", params).fetchall()

    return {
        "concetti": [{"id": c["id"], "nome": c["nome"], "note": c["note"], "tinta": colori.get(c["id"], 0),
                      "ramo": radici.get(c["id"])} for c in concetti],
        "azioni": [{"id": a["id"], "testo": a["descrizione"], "chi": a["responsabile"], "quando": a["scadenza"],
                    "stato": a["stato"], "nota": {"id": a["nota"], "titolo": a["titolo"]}} for a in azioni],
        "_tids": [c["id"] for c in concetti if c["forte"]],
    }
