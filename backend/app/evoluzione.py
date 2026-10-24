"""Come cresce la conoscenza da un vocale all'altro (lato database).

Ogni vocale lascia tracce che durano:

  - gli appunti: un blocco per argomento, collocato nel suo tema; dentro punti e
    dettagli (decisioni, domande, idee, fatti, cose da fare), ciascuno con la frase
    del vocale che lo giustifica;
  - le cose da fare (i punti "da_fare"), nel loro tema. Un impegno già aperto detto
    di nuovo non si duplica (evento "ribadita"); un vocale che dice di aver fatto una
    cosa la chiude (evento "completata" con il vocale che lo dice), e si può riaprire;
  - la scheda di ogni tema toccato: la sintesi "a che punto è" riscritta con le
    novità, e i punti di prima che il vocale rende superati.

Tutto ciò che un vocale ha fatto agli altri (chiusure, ribadite, punti superati)
è annullabile: una nuova analisi o l'eliminazione del vocale lo disfano prima di
rifarlo, così il risultato non dipende da quante volte un vocale è stato elaborato.

Le chiamate al modello stanno in pipeline/analyze.py; qui si legge e si scrive.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import psycopg

from .conoscenza import percorso, vector_literal
from .pipeline.analyze import AnalysisOutcome, Candidata, PuntoNoto, TemaIn, Voce, testo_voce
from .pipeline.text import Span, fold, stems

SOGLIA_AZIONE_UGUALE = 0.88      # coseno: stessa cosa da fare detta con altre parole
SOGLIA_CANDIDATA = 0.5           # coseno: cosa da fare di cui il vocale potrebbe parlare
MAX_CANDIDATE = 8
MAX_PUNTI_TEMA = 25


def _quando(conn: psycopg.Connection, entry_id: UUID) -> datetime | None:
    r = conn.execute("SELECT coalesce(data_ora, created_at) AS d FROM entries WHERE id = %s", (entry_id,)).fetchone()
    return r["d"] if r else None


# -- annullare ciò che un vocale ha fatto agli altri ---------------------------------------

def annulla_evoluzione(conn: psycopg.Connection, entry_ids: list[UUID]) -> None:
    """Riapre le cose chiuse automaticamente da questi vocali e rimette attivi i punti che avevano superato."""
    evs = conn.execute(
        """SELECT ev.id, ev.action_item_id FROM action_item_events ev
            WHERE ev.source_entry_id = ANY(%s) AND ev.origin = 'estratto_automatico'
              AND ev.event IN ('completata', 'annullata')""", (entry_ids,)).fetchall()
    for ev in evs:
        ultimo = conn.execute("SELECT id FROM action_item_events WHERE action_item_id = %s "
                              "ORDER BY created_at DESC, id DESC LIMIT 1", (ev["action_item_id"],)).fetchone()
        if ultimo and ultimo["id"] == ev["id"]:
            conn.execute("UPDATE action_items SET stato = 'aperta' WHERE id = %s", (ev["action_item_id"],))
    conn.execute("DELETE FROM action_item_events WHERE id = ANY(%s)", ([e["id"] for e in evs],))
    conn.execute("UPDATE punti SET stato = 'attivo', superato_da = NULL WHERE superato_da = ANY(%s)", (entry_ids,))


def annulla_ribadite(conn: psycopg.Connection, entry_ids: list[UUID]) -> None:
    conn.execute("DELETE FROM action_item_events WHERE source_entry_id = ANY(%s) AND event = 'ribadita'",
                 (entry_ids,))


def prima_di_eliminare(conn: psycopg.Connection, entry_ids: list[UUID]) -> None:
    """Un vocale che sparisce non lascia chiusure, ribadite o punti superati negli altri."""
    if entry_ids:
        annulla_evoluzione(conn, entry_ids)
        annulla_ribadite(conn, entry_ids)


def glossario(conn: psycopg.Connection, max_chars: int = 300) -> str:
    """Nomi che il riconoscimento vocale deve scrivere bene: voci con un nome, i temi più ampi, i temi recenti."""
    nomi: list[str] = []
    for r in conn.execute("""SELECT DISTINCT value AS n FROM entries e, jsonb_each_text(e.voci_nomi)
                              WHERE e.deleted_at IS NULL""").fetchall():
        nomi.append(r["n"])
    for r in conn.execute("SELECT label FROM topics WHERE parent_id IS NULL AND merged_into IS NULL "
                          "ORDER BY created_at LIMIT 12").fetchall():
        nomi.append(r["label"])
    for r in conn.execute(
            """SELECT t.label, max(coalesce(e.data_ora, e.created_at)) AS ultima FROM topics t
                 JOIN entry_topics et ON et.topic_id = t.id JOIN entries e ON e.id = et.entry_id
                WHERE t.merged_into IS NULL AND e.deleted_at IS NULL
                GROUP BY t.id ORDER BY ultima DESC LIMIT 40""").fetchall():
        nomi.append(r["label"])
    out: list[str] = []
    visti: set[str] = set()
    lung = 0
    for n in nomi:
        k = fold(n)
        if not n.strip() or k in visti:
            continue
        if lung + len(n) + 2 > max_chars:
            break
        visti.add(k)
        out.append(n.strip())
        lung += len(n) + 2
    return ", ".join(out)


def unisci_glossari(*glossari: str, max_chars: int = 300) -> str:
    out: list[str] = []
    visti: set[str] = set()
    lung = 0
    for g in glossari:
        for n in (x.strip() for x in g.split(",")):
            if not n or fold(n) in visti or lung + len(n) + 2 > max_chars:
                continue
            visti.add(fold(n))
            out.append(n)
            lung += len(n) + 2
    return ", ".join(out)


# -- salvataggio degli appunti -------------------------------------------------------------

def _testi(outcome: AnalysisOutcome) -> list[str]:
    """I testi da vettorizzare, nell'ordine in cui salva_appunti li usa."""
    return [v.testo for b in outcome.blocchi for p in b.punti for v in [p, *p.dettagli]]


testi_da_vettorizzare = _testi


def salva_appunti(conn: psycopg.Connection, owner: UUID, entry_id: UUID, version: int, outcome: AnalysisOutcome,
                  temi: list[UUID | None], vettori: list[list[float]], testo: str) -> list[str]:
    """Gli appunti del vocale, ciascun blocco nel suo tema; le cose da fare legate a quelle da spuntare.

    Una cosa da fare di questo vocale già toccata (spuntata a mano o ripetuta in un altro
    vocale) resta: se la nuova analisi la ritrova, ne prende il testo nuovo.
    """
    avvisi: list[str] = []
    annulla_ribadite(conn, [entry_id])
    conn.execute("DELETE FROM punti WHERE entry_id = %s", (entry_id,))
    conn.execute("""DELETE FROM action_items a WHERE a.entry_id = %s AND a.origin = 'estratto_automatico'
                      AND a.stato = 'aperta'
                      AND NOT EXISTS (SELECT 1 FROM action_item_events ev WHERE ev.action_item_id = a.id)""",
                 (entry_id,))
    tenute = {r["id"]: r for r in conn.execute(
        "SELECT id, descrizione, char_start, char_end, embedding IS NOT NULL AS v FROM action_items "
        "WHERE entry_id = %s", (entry_id,)).fetchall()}
    usate: set[UUID] = set()
    quando = _quando(conn, entry_id)
    k = 0
    ord_ = 0
    for bi, b in enumerate(outcome.blocchi):
        tid = temi[bi] if bi < len(temi) else None
        for p in b.punti:
            pid = None
            for livello, v in [(1, p), *[(2, d) for d in p.dettagli]]:
                vec = vettori[k] if k < len(vettori) else None
                k += 1
                lit = vector_literal(vec) if vec else None
                azione = None
                if v.tipo == "da_fare":
                    azione, avviso = _cosa_da_fare(conn, owner, entry_id, version, v, tid, lit, testo, tenute,
                                                   usate, quando, ord_)
                    if avviso:
                        avvisi.append(avviso)
                row = conn.execute(
                    """INSERT INTO punti (owner_id, entry_id, topic_id, tipo, testo, char_start, char_end, quote, ord,
                                          embedding, blocco, parent_id, action_item_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s) RETURNING id""",
                    (owner, entry_id, tid, v.tipo, v.testo, v.fonte.start if v.fonte else None,
                     v.fonte.end if v.fonte else None, v.fonte.text(testo) if v.fonte else None, ord_, lit, bi,
                     pid if livello == 2 else None, azione)).fetchone()
                ord_ += 1
                if livello == 1:
                    pid = row["id"]
    return avvisi


def _cosa_da_fare(conn: psycopg.Connection, owner: UUID, entry_id: UUID, version: int, v: Voce, tid: UUID | None,
                  lit: str | None, testo: str, tenute: dict, usate: set[UUID], quando: datetime | None,
                  ord_: int) -> tuple[UUID | None, str | None]:
    src = v.fonte
    quote = src.text(testo) if src else None
    # 1. Una cosa da fare di questo vocale tenuta da prima: stessa frase o stesso significato.
    for aid, t in tenute.items():
        if aid in usate:
            continue
        stessa = fold(t["descrizione"]) == fold(v.testo) or (
            src is not None and t["char_start"] is not None and src.start < t["char_end"] and t["char_start"] < src.end)
        if not stessa and lit and t["v"]:
            stessa = conn.execute("SELECT embedding <=> %s::vector <= %s AS ok FROM action_items WHERE id = %s",
                                  (lit, 1 - SOGLIA_AZIONE_UGUALE, aid)).fetchone()["ok"]
        if stessa:
            usate.add(aid)
            conn.execute(
                """UPDATE action_items SET descrizione = %s, scadenza = coalesce(%s, scadenza), char_start = %s,
                          char_end = %s, quote = %s, topic_id = coalesce(%s, topic_id), embedding = coalesce(%s::vector,
                          embedding), entry_version = %s, ord = %s WHERE id = %s""",
                (v.testo, v.quando, src.start if src else None, src.end if src else None, quote, tid, lit, version,
                 ord_, aid))
            return aid, None
    # 2. Un impegno già aperto da un vocale precedente: ribadito, non duplicato.
    gia = conn.execute(
        """SELECT a.id, a.scadenza, a.topic_id FROM action_items a
             JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL
            WHERE a.stato = 'aperta' AND a.entry_id <> %(e)s
              AND coalesce(e.data_ora, e.created_at) <= %(q)s
              AND (lower(a.descrizione) = lower(%(d)s)
                   OR (%(v)s::vector IS NOT NULL AND a.embedding IS NOT NULL
                       AND a.embedding <=> %(v)s::vector <= %(dist)s))
            ORDER BY (lower(a.descrizione) = lower(%(d)s)) DESC,
                     a.embedding <=> coalesce(%(v)s::vector, a.embedding) LIMIT 1""",
        {"e": entry_id, "q": quando, "d": v.testo, "v": lit, "dist": 1 - SOGLIA_AZIONE_UGUALE}).fetchone()
    if gia is not None:
        conn.execute("INSERT INTO action_item_events (owner_id, action_item_id, event, source_entry_id, note, origin) "
                     "VALUES (%s, %s, 'ribadita', %s, %s, 'estratto_automatico')",
                     (owner, gia["id"], entry_id, quote[:500] if quote else None))
        if v.quando and not gia["scadenza"]:
            conn.execute("UPDATE action_items SET scadenza = %s WHERE id = %s", (v.quando, gia["id"]))
        if tid and gia["topic_id"] is None:
            conn.execute("UPDATE action_items SET topic_id = %s WHERE id = %s", (tid, gia["id"]))
        return gia["id"], f"cosa da fare già aperta da un vocale precedente, non duplicata: {v.testo!r}"
    # 3. Nuova.
    aid = conn.execute(
        """INSERT INTO action_items (owner_id, entry_id, entry_version, descrizione, scadenza, char_start, char_end,
                                     quote, origin, ord, topic_id, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'estratto_automatico', %s, %s, %s::vector) RETURNING id""",
        (owner, entry_id, version, v.testo, v.quando, src.start if src else None, src.end if src else None, quote,
         ord_, tid, lit)).fetchone()["id"]
    usate.add(aid)
    return aid, None


def appunti_testo(conn: psycopg.Connection, entry_id: UUID, topic_id: UUID | None = None) -> str:
    """Gli appunti salvati di un vocale (di un suo tema), come elenco rientrato."""
    righe = []
    q = "SELECT id, tipo, testo, parent_id FROM punti WHERE entry_id = %s"
    params: list = [entry_id]
    if topic_id is not None:
        q += " AND topic_id = %s"
        params.append(topic_id)
    rows = conn.execute(q + " ORDER BY ord", params).fetchall()
    for r in rows:
        v = Voce(r["tipo"], r["testo"], None, None)
        righe.append(testo_voce(v, "   " if r["parent_id"] else ""))
    return "\n".join(righe)


# -- cose fatte -----------------------------------------------------------------------------

def candidate_chiusure(conn: psycopg.Connection, entry_id: UUID, testo: str,
                       passaggi: list[tuple[Span, list[float]]]) -> list[tuple[UUID, Candidata]]:
    """Cose da fare aperte, di vocali precedenti, di cui questo vocale sembra parlare.

    Per ciascuna il passaggio del vocale più simile: il modello vede solo quelli.
    """
    quando = _quando(conn, entry_id)
    best: dict[UUID, tuple[float, Span, str]] = {}
    for sp, v in passaggi:
        lit = vector_literal(v)
        for r in conn.execute(
                """SELECT a.id, a.descrizione, 1 - (a.embedding <=> %(v)s::vector) AS sim
                     FROM action_items a JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL
                    WHERE a.stato = 'aperta' AND a.entry_id <> %(e)s AND a.embedding IS NOT NULL
                      AND coalesce(e.data_ora, e.created_at) <= %(q)s
                    ORDER BY a.embedding <=> %(v)s::vector LIMIT 6""",
                {"v": lit, "e": entry_id, "q": quando}).fetchall():
            parole = stems(r["descrizione"])
            comuni = len(parole & stems(sp.text(testo))) / len(parole) if parole else 0
            score = max(r["sim"], 0.5 + comuni / 2 if comuni >= 0.5 else 0)
            if score >= SOGLIA_CANDIDATA and score > best.get(r["id"], (0.0,))[0]:
                best[r["id"]] = (score, sp, r["descrizione"])
    ordinate = sorted(best.items(), key=lambda kv: -kv[1][0])[:MAX_CANDIDATE]
    return [(aid, Candidata(desc, sp)) for aid, (_, sp, desc) in ordinate]


def chiudi(conn: psycopg.Connection, owner: UUID, action_id: UUID, esito: str, entry_id: UUID, quote: str) -> bool:
    stato = "annullata" if esito == "annullata" else "completata"
    r = conn.execute("UPDATE action_items SET stato = %s WHERE id = %s AND stato = 'aperta' RETURNING id",
                     (stato, action_id)).fetchone()
    if r is None:
        return False
    conn.execute("INSERT INTO action_item_events (owner_id, action_item_id, event, source_entry_id, note, origin) "
                 "VALUES (%s, %s, %s, %s, %s, 'estratto_automatico')",
                 (owner, action_id, stato, entry_id, quote[:500]))
    return True


# -- la scheda viva di un tema --------------------------------------------------------------

def temi_da_aggiornare(conn: psycopg.Connection, entry_id: UUID) -> list[tuple[UUID, TemaIn, list[UUID]]]:
    """Per ogni tema del vocale, ciò che serve al modello per riscriverne la scheda.

    Si aggiorna solo un tema di cui questo è il vocale più recente (o che non ha ancora
    una sintesi): rianalizzare un vocale vecchio non deve riportare il tema indietro.
    Restituisce (tema, dati, id dei punti numerati nei dati).
    """
    e = conn.execute("""SELECT id, titolo, executive_summary, coalesce(data_ora, created_at) AS d, data_ora
                          FROM entries WHERE id = %s""", (entry_id,)).fetchone()
    if e is None or not e["titolo"]:
        return []
    temi = conn.execute(
        """SELECT DISTINCT t.id, t.label, t.tipo, t.sintesi FROM entry_topics et
             JOIN topics x ON x.id = et.topic_id JOIN topics t ON t.id = coalesce(x.merged_into, x.id)
            WHERE et.entry_id = %s""", (entry_id,)).fetchall()
    out = []
    for t in temi:
        dopo = conn.execute(
            """SELECT 1 FROM entry_topics et JOIN topics x ON x.id = et.topic_id
                 JOIN entries o ON o.id = et.entry_id AND o.deleted_at IS NULL
                WHERE coalesce(x.merged_into, x.id) = %s AND o.id <> %s
                  AND coalesce(o.data_ora, o.created_at) > %s LIMIT 1""", (t["id"], entry_id, e["d"])).fetchone()
        if dopo is not None and t["sintesi"]:
            continue
        punti = conn.execute(
            """SELECT * FROM (
                 SELECT p.id, p.tipo, p.testo, coalesce(o.data_ora, o.created_at) AS d, p.ord
                   FROM punti p JOIN entries o ON o.id = p.entry_id AND o.deleted_at IS NULL
                  WHERE p.topic_id = %s AND p.stato = 'attivo' AND p.entry_id <> %s AND p.tipo <> 'da_fare'
                    AND coalesce(o.data_ora, o.created_at) <= %s
                  ORDER BY d DESC, p.ord LIMIT %s) x ORDER BY d, ord""",
            (t["id"], entry_id, e["d"], MAX_PUNTI_TEMA)).fetchall()
        aperte = conn.execute("SELECT descrizione FROM action_items WHERE topic_id = %s AND stato = 'aperta' "
                              "AND entry_id <> %s ORDER BY created_at LIMIT 12", (t["id"], entry_id)).fetchall()
        cammino = percorso(conn, t["id"])
        dati = TemaIn(
            nome=t["label"], tipo=t["tipo"], sintesi=t["sintesi"],
            punti=[PuntoNoto(p["tipo"], p["testo"], p["d"]) for p in punti],
            aperte=[a["descrizione"] for a in aperte],
            nota_titolo=e["titolo"], nota_data=e["data_ora"] or e["d"],
            nota_appunti=appunti_testo(conn, entry_id, t["id"]) or "(nessun appunto)",
            percorso=" › ".join(x["nome"] for x in cammino[:-1]) or None)
        out.append((t["id"], dati, [p["id"] for p in punti]))
    return out


def salva_tema(conn: psycopg.Connection, topic_id: UUID, entry_id: UUID, sintesi: str, superati: list[UUID],
               profilo: list[float] | None) -> None:
    conn.execute("UPDATE topics SET sintesi = %s, sintesi_il = now(), profilo = coalesce(%s::vector, profilo) "
                 "WHERE id = %s", (sintesi, vector_literal(profilo) if profilo else None, topic_id))
    if superati:
        conn.execute("UPDATE punti SET stato = 'superato', superato_da = %s WHERE id = ANY(%s) AND stato = 'attivo'",
                     (entry_id, superati))


def azzera(conn: psycopg.Connection, tieni_appunti: bool = False) -> None:
    """Per ricostruire la conoscenza da capo: niente effetti automatici tra vocali, niente albero.

    Con tieni_appunti gli appunti restano (si rifà solo la collocazione): perdono il loro
    tema finché non vengono collocati di nuovo. I percorsi confermati dalla persona e le
    sue indicazioni restano sempre: la ricostruzione li riapplica.
    """
    evs = conn.execute("SELECT DISTINCT source_entry_id AS e FROM action_item_events "
                       "WHERE origin = 'estratto_automatico' AND source_entry_id IS NOT NULL").fetchall()
    prima_di_eliminare(conn, [r["e"] for r in evs])
    if tieni_appunti:
        conn.execute("UPDATE punti SET topic_id = NULL, stato = 'attivo', superato_da = NULL")
        conn.execute("UPDATE action_items SET topic_id = NULL")
        conn.execute("UPDATE blocchi SET topic_id = NULL")
    else:
        conn.execute("DELETE FROM punti")
        conn.execute("DELETE FROM blocchi WHERE confermato IS NULL")
    conn.execute("UPDATE chiarimenti SET stato = 'superata' WHERE stato = 'aperta'")
    conn.execute("DELETE FROM entry_topics WHERE origin = 'estratto_automatico'")
    conn.execute("UPDATE topics SET sintesi = NULL, sintesi_il = NULL, profilo = NULL")
    conn.execute("UPDATE stato_albero SET note_dal_riordino = 0, riordinato_il = NULL")


def testo_profilo(nome: str, sintesi: str) -> str:
    return f"{nome}. {sintesi}"


__all__ = ["unisci_glossari", "annulla_evoluzione", "annulla_ribadite", "appunti_testo", "azzera",
           "candidate_chiusure", "chiudi", "glossario", "prima_di_eliminare", "salva_appunti", "salva_tema",
           "temi_da_aggiornare", "testi_da_vettorizzare", "testo_profilo"]
