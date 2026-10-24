"""Domande in linguaggio naturale sull'archivio.

Una domanda costa una sola chiamata al modello. Prima, in Postgres (millisecondi):
  0. le schede dei temi più vicini alla domanda (a che punto sono, decisioni, domande
     aperte, cose da fare), se la domanda non chiede un periodo;
  1. il periodo, se la domanda lo dice ("ieri", "la settimana scorsa", "a settembre");
  2. i passaggi delle trascrizioni più pertinenti, per parole e per significato
     (fusi come nella ricerca), al massimo due per nota;
  3. se la domanda riguarda cose da fare, quelle ancora aperte.
Se non c'è niente di pertinente si risponde subito, senza disturbare il modello.
Il modello risponde solo con i passaggi numerati e li cita come [n]: ogni [n]
porta alla nota, nel punto dell'audio in cui se ne parla.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

import psycopg

from . import search
from .pipeline.text import content_words, fold

MAX_PASSAGGI = 8
MAX_CARATTERI = 7000
MAX_PER_NOTA = 2
MAX_DISTANZA = 0.65          # più largo della ricerca: una domanda somiglia meno al testo
MAX_AZIONI = 12

_GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
         "ottobre", "novembre", "dicembre"]
_FMT_GIORNO = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
_FMT_MESE = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]

# Parole che dicono il periodo o la forma della domanda, non il suo contenuto.
_NON_CONTENUTO = set(_GIORNI + _MESI + """
oggi ieri altro avantieri settimana mese anno scorsa scorso ultima ultimo ultimi ultime giorni giorno
quest questo questa cosa cose quale quali quando come perche quanto quanti quante dove detto dicevo
parlato parlavo sono stato stata fatto ricordi ricordo dimmi sai mai tutto tutte tutti
""".split())

_AZIONI_RE = re.compile(r"\b(da fare|devo|dovevo|dobbiamo|faccio|fare|impegn|scadenz|promess|azion|compit|"
                        r"rimast|in sospeso|aperte|ricordarmi)", re.I)


@dataclass
class Periodo:
    dal: datetime
    al: datetime
    nome: str


def periodo(domanda: str, ora: datetime) -> Periodo | None:
    """Il periodo detto nella domanda, nel fuso di chi chiede (ora ha l'offset del telefono)."""
    q = fold(domanda).replace("'", " ")
    oggi = ora.replace(hour=0, minute=0, second=0, microsecond=0)
    giorno = timedelta(days=1)

    def p(dal: datetime, al: datetime, nome: str) -> Periodo:
        return Periodo(dal, al, nome)

    if re.search(r"\b(l\s?altro ieri|altroieri|avantieri)\b", q):
        return p(oggi - 2 * giorno, oggi - giorno, "l'altro ieri")
    if re.search(r"\bieri\b", q):
        return p(oggi - giorno, oggi, "ieri")
    if re.search(r"\b(oggi|stamattina|stasera|stamani|stanotte)\b", q):
        return p(oggi, oggi + giorno, "oggi")
    lun = oggi - timedelta(days=oggi.weekday())
    if re.search(r"\b(settimana scorsa|scorsa settimana)\b", q):
        return p(lun - 7 * giorno, lun, "la settimana scorsa")
    if re.search(r"\b(questa settimana|in settimana)\b", q):
        return p(lun, oggi + giorno, "questa settimana")
    m = re.search(r"\bultim[ie] (\d{1,3}) giorni\b", q)
    if m:
        return p(oggi - (int(m.group(1)) - 1) * giorno, oggi + giorno, f"gli ultimi {m.group(1)} giorni")
    if re.search(r"\b(ultima settimana|ultimi giorni|ultimi sette giorni)\b", q):
        return p(oggi - 6 * giorno, oggi + giorno, "l'ultima settimana")
    inizio_mese = oggi.replace(day=1)
    if re.search(r"\b(mese scorso|scorso mese)\b", q):
        prima = (inizio_mese - giorno).replace(day=1)
        return p(prima, inizio_mese, "il mese scorso")
    if re.search(r"\b(questo mese|ultimo mese)\b", q):
        return p(inizio_mese if "questo" in q else oggi - 30 * giorno, oggi + giorno, "questo mese")
    if re.search(r"\b(quest anno|questo anno)\b", q):
        return p(oggi.replace(month=1, day=1), oggi + giorno, "quest'anno")
    for i, g in enumerate(_GIORNI):
        if re.search(rf"\b{g}\b", q):
            indietro = (oggi.weekday() - i) % 7 or (7 if "scors" in q else 0)
            d = oggi - indietro * giorno
            return p(d, d + giorno, f"{g[:-1]}ì" if g.endswith("di") else g)
    for i, mese in enumerate(_MESI, 1):
        if re.search(rf"\b{mese}\b", q):
            anno = oggi.year if i <= oggi.month else oggi.year - 1
            dal = oggi.replace(year=anno, month=i, day=1)
            al = dal.replace(year=anno + (i == 12), month=i % 12 + 1)
            return p(dal, al, mese)
    return None


@dataclass
class Fonte:
    n: int
    entry_id: UUID
    titolo: str
    data: datetime
    testo: str
    secondi: float | None = None
    tipo: str = "passaggio"          # passaggio | sintesi | azione | tema
    tema_id: UUID | None = None

    def json(self) -> dict:
        return {"n": self.n, "entry_id": self.entry_id, "titolo": self.titolo, "data": self.data,
                "estratto": " ".join(self.testo.split())[:220], "secondi": self.secondi, "tipo": self.tipo,
                "tema_id": self.tema_id}


@dataclass
class Contesto:
    fonti: list[Fonte] = field(default_factory=list)
    periodo: Periodo | None = None

    def vuoto(self) -> bool:
        return not self.fonti


def _quando(d: datetime) -> str:
    return f"{_FMT_GIORNO[d.weekday()]} {d.day} {_FMT_MESE[d.month - 1]} {d.year}"


def _filtro(ramo: UUID | None, dal: datetime | None, al: datetime | None) -> str:
    """Filtro SQL sulle note (alias e): periodo, e un ramo dell'albero (il tema e i suoi sotto-temi)."""
    f = ""
    if dal is not None:
        f += " AND coalesce(e.data_ora, e.created_at) >= %(dal)s"
    if al is not None:
        f += " AND coalesce(e.data_ora, e.created_at) < %(al)s"
    if ramo is not None:
        f += """ AND EXISTS (SELECT 1 FROM entry_topics et JOIN topics x ON x.id = et.topic_id
                            WHERE et.entry_id = e.id AND coalesce(x.merged_into, x.id) IN (WITH RECURSIVE giu AS (SELECT %(ramo)s::uuid AS id UNION SELECT t.id FROM topics t JOIN giu ON t.parent_id = giu.id)
                              SELECT id FROM giu))"""
    return f


def raccogli(conn: psycopg.Connection, domanda: str, ora: datetime, ramo: UUID | None = None,
             dal: datetime | None = None) -> Contesto:
    per = None if dal is not None else periodo(domanda, ora)
    if per is not None:
        dal = per.dal
    al = per.al if per else None
    ctx = Contesto(periodo=per)
    filtro = _filtro(ramo, dal, al)
    parole = [w for w in content_words(domanda) if w not in _NON_CONTENUTO]
    params: dict = {"dal": dal, "al": al, "ramo": ramo, "k": search.RRF_K,
                    "orq": " or ".join(parole), "maxd": MAX_DISTANZA}

    emb = search.embed_query(domanda) if parole or per is None else None
    liste = []
    if parole:
        liste.append(f"""SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.search_tsv, tq.q) DESC) AS r
                           FROM text_chunks c JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL,
                                (SELECT websearch_to_tsquery('italian', %(orq)s) AS q) tq
                          WHERE c.search_tsv @@ tq.q {filtro}""")
    if emb is not None:
        params["emb"] = search._vec(emb)
        liste.append(f"""SELECT id, r FROM (
                           SELECT c.id, c.embedding <=> %(emb)s::vector AS d,
                                  row_number() OVER (ORDER BY c.embedding <=> %(emb)s::vector) AS r
                             FROM text_chunks c JOIN entries e ON e.id = c.entry_id AND e.deleted_at IS NULL
                            WHERE c.embedding IS NOT NULL {filtro}
                            ORDER BY c.embedding <=> %(emb)s::vector LIMIT 60) v WHERE d <= %(maxd)s""")
    usati: dict[UUID, int] = {}
    totale = 0
    # Prima le schede dei temi di cui chiede: dicono a che punto è, non solo cosa è stato detto una volta.
    if emb is not None and ramo is None and per is None:
        for t in temi_pertinenti(conn, params["emb"]):
            ctx.fonti.append(Fonte(len(ctx.fonti) + 1, t["entry_id"], t["label"], t["data"], t["testo"],
                                   tipo="tema", tema_id=t["id"]))
            totale += len(t["testo"])
    if liste:
        unione = " UNION ALL ".join(f"(SELECT id, r FROM ({x}) l{i} WHERE r <= 60)" for i, x in enumerate(liste))
        righe = conn.execute(f"""
            SELECT c.entry_id, c.content, c.char_start, e.titolo, coalesce(e.data_ora, e.created_at) AS data,
                   sum(1.0 / (%(k)s + u.r)) AS s
              FROM ({unione}) u JOIN text_chunks c ON c.id = u.id JOIN entries e ON e.id = c.entry_id
             GROUP BY c.id, c.entry_id, c.content, c.char_start, e.titolo, e.data_ora, e.created_at
             ORDER BY s DESC LIMIT 30""", params).fetchall()
        for r in righe:
            if len(ctx.fonti) >= MAX_PASSAGGI or totale + len(r["content"]) > MAX_CARATTERI:
                break
            if usati.get(r["entry_id"], 0) >= MAX_PER_NOTA:
                continue
            usati[r["entry_id"]] = usati.get(r["entry_id"], 0) + 1
            totale += len(r["content"])
            ctx.fonti.append(Fonte(len(ctx.fonti) + 1, r["entry_id"], r["titolo"] or "Nota", r["data"], r["content"],
                                   search.seconds_at(conn, r["entry_id"], r["char_start"])))

    # "Cosa ho fatto ieri?": niente parole di contenuto, ma un periodo. Si leggono le sintesi.
    if not ctx.fonti and per is not None:
        for r in conn.execute(f"""SELECT e.id, e.titolo, coalesce(e.data_ora, e.created_at) AS data,
                                         e.executive_summary FROM entries e
                                   WHERE e.deleted_at IS NULL AND e.titolo IS NOT NULL {filtro}
                                   ORDER BY data LIMIT %(lim)s""", {**params, "lim": MAX_PASSAGGI}).fetchall():
            ctx.fonti.append(Fonte(len(ctx.fonti) + 1, r["id"], r["titolo"], r["data"],
                                   " ".join(r["executive_summary"] or []), tipo="sintesi"))

    if _AZIONI_RE.search(domanda):
        for a in conn.execute(f"""SELECT a.descrizione, a.responsabile, a.scadenza, a.char_start, e.id, e.titolo,
                                         coalesce(e.data_ora, e.created_at) AS data
                                    FROM action_items a JOIN entries e ON e.id = a.entry_id AND e.deleted_at IS NULL
                                   WHERE a.stato = 'aperta' {_filtro(ramo, None, None)}
                                   ORDER BY data DESC LIMIT %(lim)s""", {**params, "lim": MAX_AZIONI}).fetchall():
            extra = ", ".join(x for x in (a["responsabile"], a["scadenza"]) if x)
            ctx.fonti.append(Fonte(len(ctx.fonti) + 1, a["id"], a["titolo"] or "Nota", a["data"],
                                   a["descrizione"] + (f" ({extra})" if extra else ""),
                                   search.seconds_at(conn, a["id"], a["char_start"]), tipo="azione"))
    return ctx


MAX_TEMI = 2
SOGLIA_TEMA = 0.5


def temi_pertinenti(conn: psycopg.Connection, emb: str) -> list[dict]:
    """I temi più vicini alla domanda, con la scheda: sintesi, decisioni e domande aperte, cose da fare."""
    out = []
    for t in conn.execute(
            """SELECT id, label, sintesi, 1 - (profilo <=> %s::vector) AS sim FROM topics
                WHERE merged_into IS NULL AND profilo IS NOT NULL AND sintesi IS NOT NULL
                ORDER BY profilo <=> %s::vector LIMIT %s""", (emb, emb, MAX_TEMI)).fetchall():
        if t["sim"] < SOGLIA_TEMA:
            continue
        ultima = conn.execute(
            """SELECT e.id, coalesce(e.data_ora, e.created_at) AS d FROM entry_topics et
                 JOIN topics x ON x.id = et.topic_id JOIN entries e ON e.id = et.entry_id AND e.deleted_at IS NULL
                WHERE coalesce(x.merged_into, x.id) = %s ORDER BY d DESC LIMIT 1""", (t["id"],)).fetchone()
        if ultima is None:
            continue
        righe = [t["sintesi"]]
        punti = conn.execute("""SELECT tipo, testo FROM punti WHERE topic_id = %s AND stato = 'attivo'
                                   AND tipo IN ('decisione', 'domanda') ORDER BY created_at DESC LIMIT 4""",
                             (t["id"],)).fetchall()
        righe += [f"{'Deciso' if p['tipo'] == 'decisione' else 'Da capire'}: {p['testo']}" for p in punti]
        aperte = conn.execute("SELECT descrizione FROM action_items WHERE topic_id = %s AND stato = 'aperta' "
                              "ORDER BY created_at DESC LIMIT 4", (t["id"],)).fetchall()
        if aperte:
            righe.append("Da fare: " + "; ".join(a["descrizione"] for a in aperte))
        out.append({"id": t["id"], "label": t["label"], "entry_id": ultima["id"], "data": ultima["d"],
                    "testo": "\n".join(righe)})
    return out


SYSTEM = """Rispondi alle domande di una persona sul suo diario di note vocali, usando SOLO gli estratti numerati che ricevi.
- Italiano, dando del tu ("hai detto…", "devi…"). Breve: una o due frasi; al massimo un elenco di pochi punti se chiede più cose.
- Dopo ogni informazione metti il numero dell'estratto tra parentesi quadre, es. [2]. Mai un numero che non c'è.
- Gli estratti sono trascrizioni automatiche: possono avere errori. Non inventare niente che non ci sia.
- Se gli estratti non rispondono alla domanda, dillo in una frase ("Nel diario non trovo…") senza aggiungere altro.
- Niente premesse, niente saluti, niente consigli non richiesti."""


def messaggi(domanda: str, ctx: Contesto, ora: datetime) -> list[dict]:
    blocchi = []
    for f in ctx.fonti:
        tipo = {"azione": "cosa da fare ancora aperta", "sintesi": "sintesi della nota",
                "tema": "scheda del tema, aggiornata all'ultimo vocale"}.get(f.tipo, "dalla trascrizione")
        blocchi.append(f"[{f.n}] {_quando(f.data)} · «{f.titolo}» · {tipo}\n{' '.join(f.testo.split())}")
    periodo_txt = f" Periodo della domanda: {ctx.periodo.nome}." if ctx.periodo else ""
    user = (f"Oggi è {_quando(ora)}.{periodo_txt}\n\nEstratti:\n\n" + "\n\n".join(blocchi) +
            f"\n\nDomanda: {domanda.strip()}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def contesto_token(msgs: list[dict]) -> int:
    n = sum(len(m["content"]) for m in msgs) / 3 + 700     # domanda + risposta
    return 4096 if n <= 4096 else 8192
