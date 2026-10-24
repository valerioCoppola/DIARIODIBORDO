"""Come si forma la conoscenza: un albero di temi che nasce da ciò che si dice (lato database).

Nessuna categoria decisa in anticipo. In cima stanno i temi più ampi emersi finora
(sulla mappa, le isole), sotto le loro parti. Per ogni argomento di un vocale:

  1. il modello propone un percorso dal tema più ampio al più specifico, con quanto ne è
     sicuro (pipeline/colloca.py);
  2. qui lo si controlla con ciò che non dipende da lui: se l'argomento somiglia poco a
     ciò che il tema scelto raccoglie, o un altro tema gli somiglia chiaramente di più, o
     un tema nuovo ha quasi il nome di uno esistente, la collocazione diventa incerta;
  3. una collocazione incerta fa nascere temi provvisori (sulla mappa, tratteggiati) e, fino
     a domande_per_vocale per vocale, una domanda alla persona con le opzioni probabili.
     La risposta si applica subito (o, se scritta a parole, con una nuova collocazione) e
     resta come indicazione: il modello la rilegge ogni volta, la domanda non torna;
  4. ogni tanto il riordino guarda l'albero intero e raggruppa, sposta, unisce, rinomina.

Ciò che la persona decide prevale sempre: un percorso confermato si riapplica identico
anche quando l'albero si ricostruisce da capo, un tema spostato o rinominato a mano è
"fissato" e il riordino non lo tocca.

I colori della mappa si calcolano dalla forma dell'albero (tinte()): nessuna tavolozza fissa.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .pipeline.colloca import Modifica, Nodo, Passo, Proposta, nome_valido
from .pipeline.text import fold, slugify

log = logging.getLogger("diario.conoscenza")

MAX_VICINI = 12
SOGLIA_VICINO = 0.45
SOGLIA_NOME_LESSICALE = 0.84      # stessa parola scritta male ("Open Street Map" / "OpenStreetMap")
MAX_INDICAZIONI = 30
AUREO = 137.50776405


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


# -- l'albero ----------------------------------------------------------------------------------

@dataclass
class Albero:
    radici: list[Nodo]
    per_id: dict[UUID, Nodo]
    vicini: set[UUID] = field(default_factory=set)

    def tutti(self) -> list[Nodo]:
        return list(self.per_id.values())


def carica_albero(conn: psycopg.Connection, vettori: Iterable[list[float]] | None = None) -> Albero:
    """L'albero dei temi, i più vivi prima; con i temi più vicini ad alcuni testi (vettori)."""
    righe = conn.execute(
        """SELECT t.id, t.label, t.sintesi, t.parent_id, t.fissato, t.created_at, t.slug, t.aliases,
                  count(DISTINCT e.id) AS note, max(coalesce(e.data_ora, e.created_at)) AS ultima
             FROM topics t
             LEFT JOIN entry_topics et ON et.topic_id = t.id
             LEFT JOIN entries e ON e.id = et.entry_id AND e.deleted_at IS NULL
            WHERE t.merged_into IS NULL
            GROUP BY t.id""").fetchall()
    per_id = {r["id"]: Nodo(r["id"], r["label"], r["note"], r["sintesi"], r["fissato"],
                            {r["slug"], *r["aliases"], slugify(r["label"])}) for r in righe}
    for r in conn.execute("SELECT merged_into, slug, aliases FROM topics WHERE merged_into IS NOT NULL").fetchall():
        if r["merged_into"] in per_id:
            per_id[r["merged_into"]].alias.update([r["slug"], *r["aliases"]])
    info = {r["id"]: r for r in righe}
    for r in righe:
        p = r["parent_id"]
        if p in per_id:
            n, padre = per_id[r["id"]], per_id[p]
            x: Nodo | None = padre                      # un ciclo, se mai ci fosse, si spezza qui
            while x is not None and x is not n:
                x = x.padre
            if x is None:
                n.padre = padre
                padre.figli.append(n)

    def recente(n: Nodo) -> tuple:
        u = ultima(n)
        return (u.timestamp() if u else 0.0, n.nome)

    memo: dict[UUID, datetime | None] = {}

    def ultima(n: Nodo) -> datetime | None:
        if n.id not in memo:
            date = [d for d in [info[n.id]["ultima"], *(ultima(f) for f in n.figli)] if d]
            memo[n.id] = max(date) if date else None
        return memo[n.id]

    def note_sotto(n: Nodo) -> int:
        return n.note + sum(note_sotto(f) for f in n.figli)

    for n in per_id.values():
        n.figli.sort(key=recente, reverse=True)
    totali = {n.id: note_sotto(n) for n in per_id.values()}
    for n in per_id.values():
        n.note = totali[n.id]
    radici = sorted((n for n in per_id.values() if n.padre is None), key=recente, reverse=True)

    vicini: set[UUID] = set()
    for v in vettori or []:
        lit = vector_literal(v)
        for r in conn.execute(
                """SELECT id, 1 - (coalesce(profilo, embedding) <=> %s::vector) AS sim FROM topics
                    WHERE merged_into IS NULL AND coalesce(profilo, embedding) IS NOT NULL
                    ORDER BY coalesce(profilo, embedding) <=> %s::vector LIMIT 4""", (lit, lit)).fetchall():
            if r["sim"] >= SOGLIA_VICINO and len(vicini) < MAX_VICINI:
                vicini.add(r["id"])
    return Albero(radici, per_id, vicini)


class _Annulla(Exception):
    pass


def albero_senza(conn: psycopg.Connection, entry_id: UUID, vettori: Iterable[list[float]] | None = None) -> Albero:
    """L'albero come sarebbe senza la collocazione attuale di questa nota.

    Quando una nota si colloca di nuovo, i temi nati solo da lei (magari provvisori, magari
    sbagliati) non devono sembrare temi già noti: né per il modello né per i controlli.
    """
    out: Albero | None = None
    try:
        with conn.transaction():
            conn.execute("DELETE FROM entry_topics WHERE entry_id = %s AND origin = 'estratto_automatico'", (entry_id,))
            pulisci_orfani(conn)
            out = carica_albero(conn, vettori)
            raise _Annulla
    except _Annulla:
        pass
    assert out is not None
    return out


def tinte(conn: psycopg.Connection) -> dict[UUID, int]:
    """La tinta (0–359, OKLCH) di ogni tema, calcolata dalla forma dell'albero.

    I temi in cima prendono tinte distanziate dall'angolo aureo, nell'ordine in cui sono
    nati: restano le stesse quando ne nascono altri, e due isole nate una dopo l'altra sono
    lontane. Ogni tema divide in parti uguali la sua fascia di tinte tra i sotto-temi, che
    quindi si distinguono tra loro con due come con dodici e restano "della famiglia" del
    tema che li contiene; la luminosità, che cambia con la profondità, la mette la mappa.
    """
    righe = conn.execute("SELECT id, parent_id, created_at FROM topics WHERE merged_into IS NULL "
                         "ORDER BY created_at, id").fetchall()
    ids = {r["id"] for r in righe}
    figli: dict[UUID, list[UUID]] = {}
    radici: list[UUID] = []
    for r in righe:
        if r["parent_id"] in ids and r["parent_id"] != r["id"]:
            figli.setdefault(r["parent_id"], []).append(r["id"])
        else:
            radici.append(r["id"])
    out: dict[UUID, float] = {}
    for i, t in enumerate(radici):
        out[t] = (28.0 + i * AUREO) % 360
    # La fascia di ogni isola è larga anche quando le isole sono tante: due isole possono avere
    # tinte vicine (sono separate nello spazio e ognuna ha la sua costa), mentre le parti dentro
    # la stessa isola devono distinguersi bene tra loro.
    banda = 150.0 if len(radici) <= 1 else 120.0
    visti: set[UUID] = set()

    def giu(t: UUID, h: float, b: float) -> None:
        if t in visti:
            return
        visti.add(t)
        ks = figli.get(t, [])
        m = len(ks)
        for j, k in enumerate(ks):
            hk = h + b * ((j + 0.5) / m - 0.5) if m > 1 else h
            out[k] = hk % 360
            giu(k, hk, max(30.0, min(b * 0.7, b / m * 1.3)))

    for t in radici:
        giu(t, out[t], banda)
    return {k: int(round(v)) % 360 for k, v in out.items()}


def radice_di(conn: psycopg.Connection, topic_id: UUID) -> UUID | None:
    r = conn.execute(
        """WITH RECURSIVE su AS (
               SELECT id, parent_id, 0 AS d FROM topics WHERE id = %s
               UNION ALL
               SELECT t.id, t.parent_id, su.d + 1 FROM topics t JOIN su ON t.id = su.parent_id WHERE su.d < 12)
           SELECT id FROM su ORDER BY d DESC LIMIT 1""", (topic_id,)).fetchone()
    return r["id"] if r else None


def percorso(conn: psycopg.Connection, topic_id: UUID) -> list[dict]:
    """Il tema e i suoi antenati, dalla radice: [{id, nome}, …]."""
    rows = conn.execute(
        """WITH RECURSIVE su AS (
               SELECT id, label, parent_id, 0 AS d FROM topics WHERE id = %s
               UNION ALL
               SELECT t.id, t.label, t.parent_id, su.d + 1 FROM topics t JOIN su ON t.id = su.parent_id
                WHERE su.d < 12)
           SELECT id, label FROM su ORDER BY d DESC""", (topic_id,)).fetchall()
    return [{"id": r["id"], "nome": r["label"]} for r in rows]


def discendenti(conn: psycopg.Connection, topic_id: UUID) -> list[UUID]:
    """Il tema e tutti i suoi sotto-temi."""
    return [r["id"] for r in conn.execute(
        """WITH RECURSIVE giu AS (
               SELECT id, 0 AS d FROM topics WHERE id = %s
               UNION ALL
               SELECT t.id, giu.d + 1 FROM topics t JOIN giu ON t.parent_id = giu.id WHERE giu.d < 12)
           SELECT id FROM giu""", (topic_id,)).fetchall()]


def pulisci_orfani(conn: psycopg.Connection) -> None:
    """Temi senza note né sotto-temi spariscono dalla mappa."""
    while True:
        n = conn.execute("""DELETE FROM topics t
                             WHERE NOT EXISTS (SELECT 1 FROM entry_topics et WHERE et.topic_id = t.id)
                               AND NOT EXISTS (SELECT 1 FROM topics x WHERE x.merged_into = t.id)
                               AND NOT EXISTS (SELECT 1 FROM topics f WHERE f.parent_id = t.id)""").rowcount
        if not n:
            break


def senza_embedding(conn: psycopg.Connection, limite: int = 300) -> list[tuple[UUID, str]]:
    return [(r["id"], r["label"]) for r in conn.execute(
        "SELECT id, label FROM topics WHERE embedding IS NULL AND merged_into IS NULL ORDER BY created_at LIMIT %s",
        (limite,)).fetchall()]


def salva_embedding_temi(conn: psycopg.Connection, vettori: dict[UUID, list[float]]) -> None:
    for tid, v in vettori.items():
        conn.execute("UPDATE topics SET embedding = %s::vector WHERE id = %s", (vector_literal(v), tid))


# -- indicazioni della persona -----------------------------------------------------------------

def indicazioni(conn: psycopg.Connection, limite: int = MAX_INDICAZIONI) -> list[str]:
    """Le più recenti, dalla più vecchia: quelle date dopo prevalgono, ed è l'ordine in cui si leggono."""
    rows = conn.execute("SELECT testo FROM indicazioni ORDER BY created_at DESC, id LIMIT %s", (limite,)).fetchall()
    return [r["testo"] for r in reversed(rows)]


def aggiungi_indicazione(conn: psycopg.Connection, owner: UUID, testo: str, entry_id: UUID | None = None) -> None:
    testo = " ".join(testo.split())[:600]
    if testo and not conn.execute("SELECT 1 FROM indicazioni WHERE testo = %s", (testo,)).fetchone():
        conn.execute("INSERT INTO indicazioni (owner_id, testo, entry_id) VALUES (%s, %s, %s)",
                     (owner, testo, entry_id))


def _cammino_testo(nomi: list[str]) -> str:
    return " › ".join(nomi)


# -- controllo delle proposte del modello ------------------------------------------------------

@dataclass
class Decisione:
    passi: list[Passo]
    certezza: str = "alta"
    chiarimento: dict | None = None          # {"tipo", "domanda", "opzioni"}
    confermato: list[str] | None = None      # percorso deciso dalla persona: si riapplica così com'è
    motivi: list[str] = field(default_factory=list)

    @property
    def provvisorio(self) -> bool:
        return self.certezza != "alta"


@dataclass
class Soglie:
    unione: float = 0.86
    coerenza: float = 0.40
    coerenza_nome: float = 0.30
    alternativa: float = 0.55
    margine: float = 0.10
    dimenticato: float = 0.62
    nome_simile: float = 0.80
    domande: int = 2

    @classmethod
    def da(cls, s) -> "Soglie":
        return cls(s.topic_merge_similarity, s.collocazione_coerenza, s.collocazione_coerenza_nome,
                   s.collocazione_alternativa, s.collocazione_margine, s.collocazione_dimenticato, s.nome_simile,
                   s.domande_per_vocale)


_ORDINE = {"alta": 0, "media": 1, "bassa": 2}


def _al_massimo(c: str, limite: str) -> str:
    return c if _ORDINE[c] >= _ORDINE[limite] else limite


def _passi_json(passi: list[Passo]) -> list[dict]:
    return [{"id": str(p.nodo.id) if p.nodo is not None else None, "nome": p.etichetta()} for p in passi]


def _opzione(passi: list[Passo], etichetta: str | None = None) -> dict:
    return {"etichetta": etichetta or _cammino_testo([p.etichetta() for p in passi]), "percorso": _passi_json(passi)}


def _coda_nuova(passi: list[Passo]) -> list[Passo]:
    """I passi nuovi dopo l'ultimo tema esistente (o, se sono tutti nuovi, l'ultimo: la cosa specifica)."""
    k = max((i for i, p in enumerate(passi) if p.nodo is not None), default=None)
    if k is None:
        return passi[-1:]
    return passi[k + 1:]


def _vicini_di(conn: psycopg.Connection, v: list[float], n: int = 5) -> list[tuple[UUID, float]]:
    lit = vector_literal(v)
    return [(r["id"], r["sim"]) for r in conn.execute(
        """SELECT id, 1 - (coalesce(profilo, embedding) <=> %s::vector) AS sim FROM topics
            WHERE merged_into IS NULL AND coalesce(profilo, embedding) IS NOT NULL
            ORDER BY coalesce(profilo, embedding) <=> %s::vector LIMIT %s""", (lit, lit, n)).fetchall()]


def _somiglianza(conn: psycopg.Connection, v: list[float], tid: UUID) -> tuple[float | None, bool]:
    """Quanto l'argomento somiglia a ciò che il tema raccoglie; e se il tema ha una scheda."""
    r = conn.execute("""SELECT 1 - (coalesce(profilo, embedding) <=> %s::vector) AS sim, profilo IS NOT NULL AS scheda
                          FROM topics WHERE id = %s AND coalesce(profilo, embedding) IS NOT NULL""",
                     (vector_literal(v), tid)).fetchone()
    return (r["sim"], r["scheda"]) if r else (None, False)


def _nome_quasi_uguale(conn: psycopg.Connection, nome: str, vettore: list[float] | None, albero: Albero,
                       esclusi: set[int], soglia: float) -> tuple[Nodo, float | None] | None:
    """Un tema esistente il cui nome è quasi questo (scritto o trascritto male, al singolare…),
    con la somiglianza degli embedding dei nomi se è quella ad averlo trovato."""
    chiave = fold(nome).replace(" ", "")
    migliore, punteggio = None, 0.0
    for n in albero.tutti():
        if id(n) in esclusi:
            continue
        altro = fold(n.nome).replace(" ", "")
        r = difflib.SequenceMatcher(None, chiave, altro).ratio()
        if (chiave in altro or altro in chiave) and min(len(chiave), len(altro)) >= 5:
            r = max(r, 0.9)
        if r >= SOGLIA_NOME_LESSICALE and r > punteggio:
            migliore, punteggio = n, r
    if migliore is not None:
        return migliore, None
    if vettore is not None:
        lit = vector_literal(vettore)
        for r in conn.execute(
                """SELECT id, 1 - (embedding <=> %s::vector) AS sim FROM topics
                    WHERE merged_into IS NULL AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector LIMIT 3""", (lit, lit)).fetchall():
            n = albero.per_id.get(r["id"])
            if n is not None and id(n) not in esclusi and r["sim"] >= soglia:
                return n, r["sim"]
    return None


def _riconosci(passi: list[Passo], albero: Albero) -> list[Passo]:
    """Un passo "nuovo" che ha il nome (o un alias) di un tema già lì è quel tema."""
    out: list[Passo] = []
    padre: Nodo | None = None
    ancora_esistenti = True
    for p in passi:
        if p.nodo is not None:
            out.append(p)
            padre = p.nodo
            continue
        if ancora_esistenti and p.nome:
            chiave = slugify(p.nome)
            fratelli = padre.figli if padre is not None else albero.radici
            noto = next((f for f in fratelli if chiave and chiave in f.alias), None)
            if noto is not None:
                out.append(Passo(noto))
                padre = noto
                continue
        ancora_esistenti = False
        out.append(p)
    return out


def valuta(conn: psycopg.Connection, argomenti: list[str], proposte: list[Proposta],
           vettori_blocchi: list[list[float] | None], vettori_nomi: dict[str, list[float]], albero: Albero,
           soglie: Soglie, confermati: dict[int, list[str]] | None = None,
           indicazioni_date: list[str] | None = None) -> list[Decisione]:
    """Le proposte del modello, controllate; per le incerte, le domande da fare (al massimo soglie.domande).

    Un argomento su cui la persona ha dato un'indicazione non passa per i controlli di somiglianza:
    se il modello è sicuro seguendo ciò che lei ha detto, vale la sua parola.
    """
    confermati = confermati or {}
    guidati = {i for i, a in enumerate(argomenti)
               if any(f"«{fold(a)}»" in fold(x) for x in (indicazioni_date or []))}
    decisioni: list[Decisione] = []
    for i, p in enumerate(proposte):
        if i in confermati:
            decisioni.append(Decisione([Passo(nome=x) for x in confermati[i]], "alta", None, confermati[i]))
            continue
        p = Proposta(_riconosci(p.percorso, albero), p.certezza, p.domanda, p.alternative)
        d = Decisione(list(p.percorso), p.certezza)
        if p.certezza != "alta":
            d.motivi.append("il modello non è sicuro")
        elif i in guidati:
            decisioni.append(d)
            continue
        alternative: list[Nodo] = list(p.alternative)
        sul_percorso = {id(x.nodo) for x in p.percorso if x.nodo is not None}
        e = next((x.nodo for x in reversed(p.percorso) if x.nodo is not None), None)
        v = vettori_blocchi[i] if i < len(vettori_blocchi) else None
        vicini_tutti: list[tuple[Nodo, float]] = []
        if v is not None and albero.per_id:
            vicini = [(albero.per_id[t], s) for t, s in _vicini_di(conn, v) if t in albero.per_id]
            vicini_tutti = vicini
            fuori = [(n, s) for n, s in vicini if id(n) not in sul_percorso and not (e is not None and n.contiene(e))]
            best = next(((n, s) for n, s in fuori if s >= soglie.alternativa), None)
            if e is not None:
                sim_e, scheda = _somiglianza(conn, v, e.id)
                soglia = soglie.coerenza if scheda else soglie.coerenza_nome
                if sim_e is not None and sim_e < soglia:
                    d.certezza = _al_massimo(d.certezza, "media")
                    d.motivi.append(f"somiglia poco a {e.nome} ({sim_e:.2f})")
                if sim_e is not None and best is not None and best[1] >= sim_e + soglie.margine:
                    d.certezza = _al_massimo(d.certezza, "media")
                    d.motivi.append(f"{best[0].nome} gli somiglia di più ({best[1]:.2f} contro {sim_e:.2f})")
            elif best is not None and best[1] >= soglie.dimenticato:
                d.certezza = _al_massimo(d.certezza, "media")
                d.motivi.append(f"tema nuovo, ma {best[0].nome} gli somiglia molto ({best[1]:.2f})")
            if best is not None and d.certezza != "alta" and best[0] not in alternative:
                alternative.append(best[0])
        # Un tema nuovo con quasi il nome di uno esistente: "è lo stesso?"
        for k, passo in enumerate(p.percorso):
            if passo.nodo is not None or not passo.nome:
                continue
            trovato = _nome_quasi_uguale(conn, passo.nome, vettori_nomi.get(passo.nome), albero, sul_percorso,
                                         soglie.nome_simile)
            if trovato is None:
                continue
            simile, sim = trovato
            prima = p.percorso[k - 1] if k else None
            stesso_livello = (prima is None and simile.padre is None) or (
                prima is not None and prima.nodo is not None and simile.padre is prima.nodo)
            if stesso_livello and (fold(simile.nome) == fold(passo.nome) or (sim or 0) >= soglie.unione):
                continue                      # lo stesso tema: si ritrova da solo, senza chiedere
            if simile.padre is None and not simile.fissato and prima is not None and prima.nodo is not None \
                    and not simile.contiene(prima.nodo):
                # Il tema esiste ma sta in cima: se è lui, va dentro il tema in cui lo si colloca ora.
                si = [*(Passo(x) for x in prima.nodo.cammino()), Passo(simile), *p.percorso[k + 1:]]
            else:
                si = [*(Passo(x) for x in simile.cammino()), *p.percorso[k + 1:]]
            dove = f" (dentro {simile.padre.nome})" if simile.padre is not None else ""
            d.chiarimento = {"tipo": "nome", "domanda": f"«{passo.nome}» è «{simile.nome}»{dove}?",
                             "opzioni": [{**_opzione(si, f"Sì, è {simile.nome}"), "alias": passo.nome,
                                          "tema": str(simile.id)},
                                         {**_opzione(p.percorso, "No, è un'altra cosa"),
                                          "distinto": [passo.nome, simile.nome]}]}
            d.certezza = _al_massimo(d.certezza, "media")
            d.motivi.append(f"nome quasi uguale a {simile.nome}")
            break
        if d.chiarimento is None and d.certezza != "alta" and d.passi:
            opzioni = [_opzione(d.passi)]
            coda = _coda_nuova(d.passi)
            # Le alternative: quelle del modello e dei controlli, poi i temi più vicini.
            for n, _s in vicini_tutti:
                if len(alternative) >= 2:
                    break
                if n not in alternative and id(n) not in sul_percorso and not (e is not None and n.contiene(e)):
                    alternative.append(n)
            for a in alternative:
                if a is e:
                    continue
                passi_a = [*(Passo(x) for x in a.cammino()), *coda]
                if all(o["percorso"] != _passi_json(passi_a) for o in opzioni):
                    opzioni.append(_opzione(passi_a))
            ultimo = d.passi[-1]
            if len(d.passi) > 1 and ultimo.nodo is None and len(opzioni) < 4:
                opzioni.append(_opzione([Passo(nome=ultimo.nome)], f"{ultimo.nome}, a sé"))
            domanda = p.domanda if p.domanda and p.certezza != "alta" else f"Dove va «{argomenti[i]}»?"
            # Con una sola opzione si chiede solo se il modello sta indovinando: la risposta a parole
            # (l'informazione che mancava) è proprio ciò che serve.
            if len(opzioni) > 1 or d.certezza == "bassa":
                d.chiarimento = {"tipo": "collocazione", "domanda": domanda[:300], "opzioni": opzioni[:4]}
        decisioni.append(d)
    # Solo le domande più importanti: prima quelle sui nomi, poi le meno sicure.
    con = sorted((i for i, d in enumerate(decisioni) if d.chiarimento),
                 key=lambda i: (decisioni[i].chiarimento["tipo"] != "nome", -_ORDINE[decisioni[i].certezza], i))
    for i in con[soglie.domande:]:
        decisioni[i].chiarimento = None
    return decisioni


# -- applicare una collocazione ----------------------------------------------------------------

def _slug_libero(conn: psycopg.Connection, nome: str, padre: UUID | None) -> str:
    base = slugify(nome) or "tema"
    if not conn.execute("SELECT 1 FROM topics WHERE slug = %s", (base,)).fetchone():
        return base
    if padre is not None:
        ps = conn.execute("SELECT slug FROM topics WHERE id = %s", (padre,)).fetchone()
        if ps:
            s = slugify(f"{ps['slug']} {nome}")[:60].strip("-")
            if s and not conn.execute("SELECT 1 FROM topics WHERE slug = %s", (s,)).fetchone():
                return s
    for k in range(2, 1000):
        s = f"{base[:55]}-{k}"
        if not conn.execute("SELECT 1 FROM topics WHERE slug = %s", (s,)).fetchone():
            return s
    raise RuntimeError("nessuno slug libero")


def _vivo(conn: psycopg.Connection, tid: UUID) -> UUID | None:
    r = conn.execute("SELECT coalesce(merged_into, id) AS id FROM topics WHERE id = %s", (tid,)).fetchone()
    return r["id"] if r else None


def trova_figlio(conn: psycopg.Connection, padre: UUID | None, nome: str, vettore: list[float] | None,
                 soglia: float) -> UUID | None:
    """Il tema con questo nome (o alias, o significato) allo stesso livello."""
    slug = slugify(nome)
    r = conn.execute(
        """SELECT p.id FROM topics x JOIN topics p ON p.id = coalesce(x.merged_into, x.id)
            WHERE (x.slug = %s OR %s = ANY(x.aliases) OR lower(x.label) = lower(%s))
              AND p.parent_id IS NOT DISTINCT FROM %s
            ORDER BY (x.slug = %s) DESC LIMIT 1""", (slug, slug, nome, padre, slug)).fetchone()
    if r:
        return r["id"]
    # Lo slug dei sotto-temi può avere davanti quello del padre ("casa-bagno").
    if padre is not None:
        ps = conn.execute("SELECT slug FROM topics WHERE id = %s", (padre,)).fetchone()
        if ps:
            r = conn.execute("SELECT id FROM topics WHERE slug = %s AND parent_id = %s AND merged_into IS NULL",
                             (slugify(f"{ps['slug']} {nome}"), padre)).fetchone()
            if r:
                return r["id"]
    if vettore is not None:
        lit = vector_literal(vettore)
        r = conn.execute(
            """SELECT id, label, 1 - (embedding <=> %s::vector) AS sim FROM topics
                WHERE merged_into IS NULL AND embedding IS NOT NULL AND parent_id IS NOT DISTINCT FROM %s
                ORDER BY embedding <=> %s::vector LIMIT 1""", (lit, padre, lit)).fetchone()
        if r is not None and r["sim"] >= soglia:
            if slug:
                conn.execute("UPDATE topics SET aliases = array_append(aliases, %s) "
                             "WHERE id = %s AND NOT (%s = ANY(aliases))", (slug, r["id"], slug))
            log.info("tema %r unito a %r", nome, r["label"])
            return r["id"]
    return None


def crea_tema(conn: psycopg.Connection, owner: UUID, nome: str, padre: UUID | None, vettore: list[float] | None,
              provvisorio: bool) -> UUID:
    return conn.execute(
        """INSERT INTO topics (owner_id, slug, label, embedding, parent_id, provvisorio)
           VALUES (%s, %s, %s, %s::vector, %s, %s) RETURNING id""",
        (owner, _slug_libero(conn, nome, padre), nome, vector_literal(vettore) if vettore else None, padre,
         provvisorio)).fetchone()["id"]


def applica_percorso(conn: psycopg.Connection, owner: UUID, passi: list[dict] | list[Passo], provvisorio: bool,
                     vettori_nomi: dict[str, list[float]] | None = None, soglia: float = 0.86,
                     avvisi: list[str] | None = None) -> UUID | None:
    """Il tema in fondo al percorso, creando ciò che manca; i passi sono Passo o {"id", "nome"}."""
    vettori_nomi = vettori_nomi or {}
    padre: UUID | None = None
    toccati: list[UUID] = []
    for p in passi:
        if isinstance(p, Passo):
            pid, nome = (p.nodo.id if p.nodo is not None else None), p.etichetta()
        else:
            pid, nome = (UUID(p["id"]) if p.get("id") else None), p.get("nome") or ""
        tid = _vivo(conn, pid) if pid is not None else None
        if tid is not None and padre is not None:
            r = conn.execute("SELECT parent_id, fissato FROM topics WHERE id = %s", (tid,)).fetchone()
            if r["parent_id"] is None and not r["fissato"] and padre not in discendenti(conn, tid):
                # Un tema in cima si rivela parte di una cosa più ampia: ci va dentro.
                conn.execute("UPDATE topics SET parent_id = %s WHERE id = %s", (padre, tid))
                if avvisi is not None:
                    avvisi.append(f"tema {nome!r} portato dentro un tema più ampio")
            # Altrimenti, se nel frattempo il tema è stato spostato, resta dov'è: è lui.
        if tid is None:
            n = nome_valido(nome) or (nome if not isinstance(p, Passo) and nome else None)
            if not n:
                if avvisi is not None:
                    avvisi.append(f"nome di tema non valido scartato: {nome!r}")
                continue
            tid = trova_figlio(conn, padre, n, vettori_nomi.get(n), soglia)
            if tid is None:
                tid = crea_tema(conn, owner, n, padre, vettori_nomi.get(n), provvisorio)
        toccati.append(tid)
        padre = tid
    if toccati and not provvisorio:
        conn.execute("UPDATE topics SET provvisorio = false WHERE id = ANY(%s)", (toccati,))
    return padre


def salva_collocazione(conn: psycopg.Connection, owner: UUID, entry_id: UUID, argomenti: list[str],
                       decisioni: list[Decisione], vettori_nomi: dict[str, list[float]], soglia: float
                       ) -> tuple[list[UUID | None], list[str]]:
    """Collega la nota al tema di ogni suo argomento; salva gli argomenti e le domande."""
    avvisi: list[str] = []
    conn.execute("DELETE FROM entry_topics WHERE entry_id = %s AND origin = 'estratto_automatico'", (entry_id,))
    conn.execute("UPDATE chiarimenti SET stato = 'superata' WHERE entry_id = %s AND stato = 'aperta'", (entry_id,))
    temi: list[UUID | None] = []
    for i, (arg, d) in enumerate(zip(argomenti, decisioni)):
        tid = applica_percorso(conn, owner, d.passi, d.provvisorio, vettori_nomi, soglia, avvisi)
        if d.motivi and d.provvisorio:
            avvisi.append(f"collocazione incerta di {arg!r}: " + "; ".join(d.motivi))
        temi.append(tid)
        conn.execute(
            """INSERT INTO blocchi (owner_id, entry_id, ord, argomento, topic_id, certezza, confermato)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (entry_id, ord) DO UPDATE SET argomento = EXCLUDED.argomento,
                      topic_id = EXCLUDED.topic_id, certezza = EXCLUDED.certezza, confermato = EXCLUDED.confermato""",
            (owner, entry_id, i, arg, tid, d.certezza, Jsonb(d.confermato) if d.confermato else None))
        if tid:
            conn.execute("INSERT INTO entry_topics (owner_id, entry_id, topic_id, origin) "
                         "VALUES (%s, %s, %s, 'estratto_automatico') ON CONFLICT DO NOTHING", (owner, entry_id, tid))
        if d.chiarimento:
            conn.execute("INSERT INTO chiarimenti (owner_id, entry_id, blocco, tipo, domanda, opzioni) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (owner, entry_id, i, d.chiarimento["tipo"], d.chiarimento["domanda"],
                          Jsonb(d.chiarimento["opzioni"])))
    conn.execute("DELETE FROM blocchi WHERE entry_id = %s AND ord >= %s", (entry_id, len(argomenti)))
    # Gli appunti già salvati (collocazione rifatta senza riorganizzare) seguono il loro blocco.
    for i, tid in enumerate(temi):
        conn.execute("UPDATE punti SET topic_id = %s WHERE entry_id = %s AND blocco = %s", (tid, entry_id, i))
        conn.execute("UPDATE action_items a SET topic_id = %s FROM punti p WHERE p.action_item_id = a.id "
                     "AND p.entry_id = %s AND p.blocco = %s AND a.entry_id = %s", (tid, entry_id, i, entry_id))
    pulisci_orfani(conn)
    return temi, avvisi


def confermati(conn: psycopg.Connection, entry_id: UUID, argomenti: list[str]) -> dict[int, list[str]]:
    """I percorsi decisi dalla persona per gli argomenti di questa nota (ritrovati per nome)."""
    per_nome = {fold(r["argomento"]): r["confermato"] for r in conn.execute(
        "SELECT argomento, confermato FROM blocchi WHERE entry_id = %s AND confermato IS NOT NULL",
        (entry_id,)).fetchall()}
    return {i: per_nome[fold(a)] for i, a in enumerate(argomenti) if fold(a) in per_nome}


def carica_blocchi(conn: psycopg.Connection, entry_id: UUID):
    """Gli argomenti salvati di una nota, con i loro appunti: per rifare solo la collocazione.

    None se non ci sono (o non sono in ordine): allora serve l'analisi completa.
    """
    from .pipeline.analyze import Blocco, Voce

    righe = conn.execute("SELECT ord, argomento FROM blocchi WHERE entry_id = %s ORDER BY ord", (entry_id,)).fetchall()
    if not righe or [r["ord"] for r in righe] != list(range(len(righe))):
        return None
    blocchi = [Blocco(r["argomento"], []) for r in righe]
    per_id: dict[UUID, Voce] = {}
    for p in conn.execute("SELECT id, blocco, tipo, testo, quote, char_start, char_end, parent_id FROM punti "
                          "WHERE entry_id = %s ORDER BY ord", (entry_id,)).fetchall():
        v = Voce(p["tipo"], p["testo"], None, None)
        if p["parent_id"] and p["parent_id"] in per_id:
            per_id[p["parent_id"]].dettagli.append(v)
        elif 0 <= p["blocco"] < len(blocchi):
            per_id[p["id"]] = v
            blocchi[p["blocco"]].punti.append(v)
    return blocchi if all(b.punti for b in blocchi) else None


def nomi_da_vettorizzare(decisioni_o_proposte: Iterable[Decisione | Proposta]) -> list[str]:
    out: list[str] = []
    for d in decisioni_o_proposte:
        for p in (d.passi if isinstance(d, Decisione) else d.percorso):
            if p.nodo is None and p.nome and p.nome not in out:
                out.append(p.nome)
    return out


# -- risposte e correzioni della persona -------------------------------------------------------

class Rifiutato(Exception):
    pass


def _blocco_su_tema(conn: psycopg.Connection, owner: UUID, entry_id: UUID, ordine: int, tid: UUID | None,
                    confermato: list[str] | None) -> None:
    conn.execute("UPDATE blocchi SET topic_id = %s, certezza = 'alta', confermato = %s WHERE entry_id = %s AND ord = %s",
                 (tid, Jsonb(confermato) if confermato else None, entry_id, ordine))
    conn.execute("UPDATE punti SET topic_id = %s WHERE entry_id = %s AND blocco = %s", (tid, entry_id, ordine))
    conn.execute("UPDATE action_items a SET topic_id = %s FROM punti p WHERE p.action_item_id = a.id "
                 "AND p.entry_id = %s AND p.blocco = %s AND a.entry_id = %s", (tid, entry_id, ordine, entry_id))
    conn.execute("DELETE FROM entry_topics WHERE entry_id = %s AND origin = 'estratto_automatico'", (entry_id,))
    conn.execute("""INSERT INTO entry_topics (owner_id, entry_id, topic_id, origin)
                    SELECT DISTINCT %s, entry_id, topic_id, 'estratto_automatico'::origine_info FROM blocchi
                     WHERE entry_id = %s AND topic_id IS NOT NULL ON CONFLICT DO NOTHING""", (owner, entry_id))


def rispondi(conn: psycopg.Connection, owner: UUID, chiarimento_id: UUID, scelta: int | None = None,
             testo: str | None = None) -> str:
    """Applica la risposta a una domanda. Restituisce ciò che resta da fare: "evolvi" o "colloca"."""
    c = conn.execute("SELECT c.*, b.argomento, e.titolo FROM chiarimenti c "
                     "JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL "
                     "LEFT JOIN blocchi b ON b.entry_id = c.entry_id AND b.ord = c.blocco "
                     "WHERE c.id = %s FOR UPDATE OF c", (chiarimento_id,)).fetchone()
    if c is None:
        raise Rifiutato("domanda non trovata")
    if c["stato"] != "aperta":
        raise Rifiutato("a questa domanda è già stata data una risposta")
    argomento = c["argomento"] or "questo argomento"
    if testo is not None:
        testo = " ".join(testo.split())
        if not testo:
            raise Rifiutato("risposta vuota")
        aggiungi_indicazione(conn, owner, f"Su «{argomento}»: {testo}"[:600], c["entry_id"])
        conn.execute("UPDATE chiarimenti SET stato = 'risposta', risposta = %s, risposto_il = now() WHERE id = %s",
                     (Jsonb({"testo": testo}), c["id"]))
        return "colloca"
    opzioni = c["opzioni"] or []
    if scelta is None or not 0 <= scelta < len(opzioni):
        raise Rifiutato("scelta non valida")
    passi = opzioni[scelta]["percorso"]
    nomi = [p["nome"] for p in passi]
    tid = applica_percorso(conn, owner, passi, provvisorio=False)
    op = opzioni[scelta]
    if op.get("alias") and op.get("tema"):
        # "Sì, è quello": il nome detto diventa un altro modo di chiamarlo.
        dest = _vivo(conn, UUID(op["tema"]))
        a = slugify(op["alias"])
        if dest is not None and a:
            conn.execute("UPDATE topics SET aliases = array_append(aliases, %s) "
                         "WHERE id = %s AND NOT (%s = ANY(aliases)) AND slug <> %s", (a, dest, a, a))
        aggiungi_indicazione(conn, owner, f"«{op['alias']}» è «{op['etichetta'].removeprefix('Sì, è ')}»",
                             c["entry_id"])
    if op.get("distinto"):
        detto, altro = op["distinto"]
        aggiungi_indicazione(conn, owner, f"«{detto}» non è «{altro}»: sono due cose diverse", c["entry_id"])
    aggiungi_indicazione(conn, owner, f"«{argomento}» va in: {_cammino_testo(nomi)}", c["entry_id"])
    _blocco_su_tema(conn, owner, c["entry_id"], c["blocco"], tid, nomi)
    conn.execute("UPDATE chiarimenti SET stato = 'risposta', risposta = %s, risposto_il = now() WHERE id = %s",
                 (Jsonb({"scelta": scelta}), c["id"]))
    pulisci_orfani(conn)
    return "evolvi"


def sposta_tema(conn: psycopg.Connection, owner: UUID, tid: UUID, dentro: UUID | None) -> None:
    t = conn.execute("SELECT id, label FROM topics WHERE id = %s AND merged_into IS NULL", (tid,)).fetchone()
    if t is None:
        raise Rifiutato("tema non trovato")
    nome_dentro = None
    if dentro is not None:
        d = conn.execute("SELECT id, label FROM topics WHERE id = %s AND merged_into IS NULL", (dentro,)).fetchone()
        if d is None:
            raise Rifiutato("tema di destinazione non trovato")
        if dentro in discendenti(conn, tid):
            raise Rifiutato("un tema non può andare dentro se stesso o dentro un suo sotto-tema")
        nome_dentro = d["label"]
    conn.execute("UPDATE topics SET parent_id = %s, fissato = true, provvisorio = false WHERE id = %s", (dentro, tid))
    aggiungi_indicazione(conn, owner, f"«{t['label']}» fa parte di «{nome_dentro}»" if nome_dentro
                         else f"«{t['label']}» è un tema a sé, non fa parte di altri")


def rinomina_tema(conn: psycopg.Connection, owner: UUID, tid: UUID, nome: str) -> str:
    t = conn.execute("SELECT id, label, slug, parent_id FROM topics WHERE id = %s AND merged_into IS NULL",
                     (tid,)).fetchone()
    if t is None:
        raise Rifiutato("tema non trovato")
    from .pipeline.analyze import nome_tema

    n = nome_tema(nome)
    if not n:
        raise Rifiutato("nome non valido")
    if n == t["label"]:
        return n
    gemello = conn.execute("SELECT id FROM topics WHERE merged_into IS NULL AND id <> %s AND lower(label) = lower(%s) "
                           "AND parent_id IS NOT DISTINCT FROM %s", (tid, n, t["parent_id"])).fetchone()
    if gemello:
        unisci_temi(conn, tid, gemello["id"])
        conn.execute("UPDATE topics SET fissato = true, provvisorio = false WHERE id = %s", (gemello["id"],))
    else:
        nuovo = slugify(n)
        slug = nuovo if nuovo and not conn.execute("SELECT 1 FROM topics WHERE slug = %s AND id <> %s",
                                                   (nuovo, tid)).fetchone() else _slug_libero(conn, n, t["parent_id"])
        conn.execute("UPDATE topics SET label = %s, slug = %s, embedding = NULL, fissato = true, provvisorio = false, "
                     "aliases = CASE WHEN %s = ANY(aliases) THEN aliases ELSE array_append(aliases, %s) END "
                     "WHERE id = %s", (n, slug, t["slug"], t["slug"], tid))
    aggiungi_indicazione(conn, owner, f"«{t['label']}» si chiama «{n}»")
    return n


def conferma_tema(conn: psycopg.Connection, tid: UUID) -> None:
    conn.execute("UPDATE topics SET provvisorio = false WHERE id = ANY(%s)",
                 ([x["id"] for x in percorso(conn, tid)],))


def unisci_temi(conn: psycopg.Connection, da: UUID, verso: UUID) -> None:
    """Il primo tema confluisce nel secondo: note, appunti, cose da fare, sotto-temi e nomi."""
    conn.execute("""INSERT INTO entry_topics (owner_id, entry_id, topic_id, origin)
                    SELECT owner_id, entry_id, %s, origin FROM entry_topics WHERE topic_id = %s
                    ON CONFLICT DO NOTHING""", (verso, da))
    conn.execute("DELETE FROM entry_topics WHERE topic_id = %s", (da,))
    for tabella in ("punti", "action_items", "blocchi"):
        conn.execute(f"UPDATE {tabella} SET topic_id = %s WHERE topic_id = %s", (verso, da))
    conn.execute("UPDATE topics SET parent_id = %s WHERE parent_id = %s AND id <> %s", (verso, da, verso))
    conn.execute("UPDATE topics SET merged_into = %s WHERE merged_into = %s", (verso, da))
    src = conn.execute("SELECT slug, aliases, sintesi FROM topics WHERE id = %s", (da,)).fetchone()
    if src:
        for s in [src["slug"], *src["aliases"]]:
            conn.execute("UPDATE topics SET aliases = array_append(aliases, %s) WHERE id = %s "
                         "AND NOT (%s = ANY(aliases)) AND slug <> %s", (s, verso, s, s))
        conn.execute("UPDATE topics SET sintesi = coalesce(sintesi, %s) WHERE id = %s", (src["sintesi"], verso))
    conn.execute("DELETE FROM topics WHERE id = %s", (da,))


# -- riordino ------------------------------------------------------------------------------------

def applica_riordino(conn: psycopg.Connection, owner: UUID, modifiche: list[Modifica]) -> list[str]:
    """Applica le modifiche del riordino (già controllate); restituisce cosa è cambiato."""
    fatto: list[str] = []
    for m in modifiche:
        vivi = [_vivo(conn, t.id) for t in m.temi]
        if any(v is None for v in vivi):
            continue
        if m.tipo == "raggruppa":
            padri = {conn.execute("SELECT parent_id FROM topics WHERE id = %s", (v,)).fetchone()["parent_id"]
                     for v in vivi}
            if len(padri) != 1:
                continue
            padre = padri.pop()
            nuovo = trova_figlio(conn, padre, m.nome, None, 1.1) or crea_tema(conn, owner, m.nome, padre, None, True)
            for v in vivi:
                if v != nuovo:
                    conn.execute("UPDATE topics SET parent_id = %s WHERE id = %s AND NOT fissato", (nuovo, v))
            fatto.append(f"raggruppati {[t.nome for t in m.temi]} in {m.nome!r}")
        elif m.tipo == "sposta":
            dest = _vivo(conn, m.dentro.id) if m.dentro is not None else None
            if dest is not None and dest in discendenti(conn, vivi[0]):
                continue
            conn.execute("UPDATE topics SET parent_id = %s WHERE id = %s AND NOT fissato", (dest, vivi[0]))
            fatto.append(f"{m.temi[0].nome!r} spostato " + (f"dentro {m.dentro.nome!r}" if m.dentro else "in cima"))
        elif m.tipo == "unisci":
            dest = _vivo(conn, m.dentro.id)
            if dest is None or dest == vivi[0] or dest in discendenti(conn, vivi[0]) \
                    or vivi[0] in discendenti(conn, dest):
                continue
            unisci_temi(conn, vivi[0], dest)
            fatto.append(f"{m.temi[0].nome!r} unito a {m.dentro.nome!r}")
        elif m.tipo == "rinomina":
            slug = _slug_libero(conn, m.nome, None)
            conn.execute("UPDATE topics SET label = %s, slug = %s, embedding = NULL, "
                         "aliases = array_append(aliases, slug) WHERE id = %s AND NOT fissato",
                         (m.nome, slug, vivi[0]))
            fatto.append(f"{m.temi[0].nome!r} rinominato {m.nome!r}")
        if m.motivo:
            fatto[-1] += f" ({m.motivo})"
    pulisci_orfani(conn)
    return fatto


def conta_per_riordino(conn: psycopg.Connection, owner: UUID) -> int:
    return conn.execute(
        """INSERT INTO stato_albero (owner_id, note_dal_riordino) VALUES (%s, 1)
           ON CONFLICT (owner_id, diario_id) DO UPDATE SET note_dal_riordino = stato_albero.note_dal_riordino + 1
           RETURNING note_dal_riordino""", (owner,)).fetchone()["note_dal_riordino"]


def da_riordinare(conn: psycopg.Connection, ogni: int, ore: float) -> bool:
    """Ogni `ogni` note collocate; oppure, a coda vuota, se l'ultimo riordino è vecchio."""
    s = conn.execute("SELECT note_dal_riordino AS n, riordinato_il FROM stato_albero").fetchone()
    if s is None or s["n"] <= 0:
        return False
    if s["n"] >= ogni:
        return True
    in_coda = conn.execute("SELECT 1 FROM ingestion_jobs WHERE completed_at IS NULL AND status NOT IN "
                           "('errore_definitivo', 'annullato', 'archiviazione_completata') LIMIT 1").fetchone()
    if in_coda:
        return False
    vecchio = conn.execute("SELECT riordinato_il IS NULL OR riordinato_il < now() - make_interval(secs => %s) AS v "
                           "FROM stato_albero", (ore * 3600,)).fetchone()["v"]
    return bool(vecchio)


def segna_riordino(conn: psycopg.Connection, owner: UUID) -> None:
    conn.execute("""INSERT INTO stato_albero (owner_id, note_dal_riordino, riordinato_il) VALUES (%s, 0, now())
                    ON CONFLICT (owner_id, diario_id) DO UPDATE SET note_dal_riordino = 0, riordinato_il = now()""", (owner,))
