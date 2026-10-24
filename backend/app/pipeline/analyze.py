"""Analisi con un modello locale (Ollama): i pensieri del vocale riorganizzati e collocati.

Un vocale è spesso un flusso di coscienza: salta da un argomento all'altro, ci torna,
mescola decisioni, dubbi e cose da fare. L'analisi lo riorganizza come farebbe un
buon assistente, in richieste piccole e mirate (un modello da 4–9 miliardi lavora
meglio su compiti brevi):

  1. riorganizza   gli appunti del vocale: un blocco per ogni argomento distinto (anche
                   se detto di passaggio), dentro i punti in ordine logico e per ogni punto
                   i dettagli (nomi, cifre, motivi). Ogni punto ha un tipo: decisione,
                   domanda, idea, fatto, da_fare (una cosa da fare da spuntare) o punto.
                   I vocali lunghi si riorganizzano a parti, poi le parti si uniscono.
  2. colloca       ogni blocco nell'albero dei temi, con un percorso dal tema più ampio al più
                   specifico e quanto è sicuro (vedi colloca.py, che fa anche il riordino).
  3. aggiorna_tema per ogni tema toccato: "a che punto è", riscritto con le novità, e quali
                   punti raccolti prima ora sono superati.
  4. chiusure      se ci sono cose da fare aperte vicine a ciò che si dice: quali il vocale
                   dice fatte o non più necessarie.

Regola guida: nessun fatto che non sia nella fonte. Dopo la risposta ogni punto e ogni
dettaglio devono trovare una frase della trascrizione che li giustifichi (salvata come
citazione, per riascoltare quel punto); senza riscontro vengono scartati. Una scadenza
che non compare nel testo viene tolta. Una chiusura vale solo se la frase citata è
nella trascrizione e riguarda davvero quella cosa da fare.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..note_schema import Categoria, title_word_count
from .errors import PipelineError
from .ollama import OllamaClient
from .text import Span, appears_in, best_supporting_sentence, fold, sentences, stems

log = logging.getLogger("diario.analisi")

PROMPT_VERSION = "appunti-1"
MAX_ATTEMPTS = 3
CHARS_PER_TOKEN = 3.0          # stima prudente per l'italiano
PROMPT_OVERHEAD_TOKENS = 3400  # istruzioni + risposta
MIN_CONTEXT = 4096
MAX_BLOCCHI = 8
MAX_PUNTI = 7
MAX_DETTAGLI = 5
MAX_PAROLE_DA_FARE = 8         # nel prompt 6; si chiede di accorciare oltre 8
MAX_PAROLE_ARGOMENTO = 5       # nel prompt 4
MAX_PAROLE_NOME_TEMA = 4       # nel prompt 3
TITOLO_PAROLE = (3, 7)
SUPPORT_DA_FARE = 0.25
SUPPORT_PUNTO = 0.15           # un appunto riformula: basta che una parte sia nel testo

CATEGORIE = [c.value for c in Categoria]
TIPI_TEMA = ["progetto", "idea", "tema"]
TIPI_PUNTO = ["punto", "decisione", "domanda", "idea", "fatto", "da_fare"]

_FEDELTA = """Fedeltà: usa SOLO ciò che è detto. Non aggiungere fatti, nomi, numeri, date, luoghi, cause, conclusioni o consigli. Puoi riformulare per chiarezza e correggere errori evidenti del riconoscimento vocale."""

_TIPI = """Ogni punto ha un tipo:
  - "decisione": una scelta presa;
  - "domanda": un dubbio, qualcosa ancora da capire o da decidere;
  - "idea": una proposta, un'intuizione da sviluppare;
  - "da_fare": un impegno concreto preso esplicitamente ("domani chiamo…", "devo mandare…"). Allora il testo è di 2–6 parole, verbo all'infinito + oggetto (es. "Chiamare il commercialista"), e "quando" è la scadenza con le parole del vocale, se c'è;
  - "fatto": cosa è successo, a che punto si è, un'informazione concreta;
  - "punto": tutto il resto (un ragionamento, una valutazione, una spiegazione)."""

_STRUTTURA = f"""Struttura (JSON):
- blocchi: uno per ogni argomento distinto del vocale (un progetto, un lavoro, una questione, un'idea, un episodio della giornata), dal più importante. Per ciascuno:
  - argomento: di cosa si tratta in 1–4 parole, come lo scriveresti in un indice (es. "sito cliente Rossi", "ristrutturazione bagno", "API del gestionale"); un nome, mai una frase;
  - punti: da 1 a {MAX_PUNTI} punti in ordine logico. Ognuno è una frase breve e completa, che si capisce da sola anche fra mesi, senza "ha detto che". Per ciascuno: testo, tipo, quando (solo per "da_fare", altrimenti null) e dettagli;
  - dettagli: da 0 a {MAX_DETTAGLI} specificazioni del punto (passaggi, motivi, cifre, nomi, alternative), frasi brevi, ognuna con testo, tipo e quando come sopra.

{_TIPI}"""

SYSTEM = f"""Sei l'archivista del diario personale di una persona: un "secondo cervello" alimentato dai suoi vocali, spesso flussi di coscienza.
Ricevi la trascrizione automatica di un vocale in italiano: può avere errori, ripetizioni, frasi spezzate, salti da un argomento all'altro e ritorni. Se parlano più persone, ogni intervento comincia con chi parla (es. "Voce 1:" o un nome).

Il tuo compito è RIORGANIZZARE i pensieri in appunti ordinati, come un assistente che ha capito a fondo che cosa intende la persona.

1. {_FEDELTA}
2. Completezza: ogni argomento distinto deve avere il suo blocco, anche se è detto di passaggio. Non perdere i dettagli concreti: nomi, cifre, date, luoghi, motivazioni, alternative.
3. Ordine: ciò che è detto in momenti diversi sullo stesso argomento va nello stesso blocco; dentro il blocco i punti seguono un ordine logico, non quello del parlato. Niente ripetizioni.

- titolo: da 3 a 6 parole che dicono di cosa parla il vocale, senza virgolette e senza punto finale.
- categoria: una sola tra "Lavoro", "Idea", "Riassunto giornata", "Riflessione", "Personale", "Altro".
{_STRUTTURA}

Rispondi solo con il JSON."""

SYSTEM_PARTE = f"""Sei l'archivista del diario personale di una persona.
Ricevi UNA PARTE della trascrizione automatica di un vocale lungo, in italiano. Se parlano più persone, ogni intervento comincia con chi parla.
Riorganizza i pensieri di questa parte in appunti ordinati.

1. {_FEDELTA}
2. Completezza: ogni argomento distinto di questa parte ha il suo blocco; non perdere i dettagli concreti.

- titolo: da 3 a 6 parole su questa parte.
- categoria: una sola tra "Lavoro", "Idea", "Riassunto giornata", "Riflessione", "Personale", "Altro".
{_STRUTTURA}

Rispondi solo con il JSON."""

SYSTEM_UNISCI = f"""Sei l'archivista del diario personale di una persona.
Ricevi gli appunti già riorganizzati di parti consecutive di uno stesso vocale lungo. Uniscili in un'unica riorganizzazione dell'intero vocale.

- Gli argomenti uguali o che sono la stessa cosa vanno in un solo blocco; i punti che si ripetono si uniscono; ciò che una parte successiva cambia (una decisione rivista) prevale.
- Non togliere argomenti né dettagli concreti e non aggiungere niente che non sia negli appunti.
- titolo: da 3 a 6 parole sull'intero vocale. categoria: una sola tra "Lavoro", "Idea", "Riassunto giornata", "Riflessione", "Personale", "Altro".
{_STRUTTURA}

Rispondi solo con il JSON."""

SYSTEM_TEMA = """Tieni aggiornata la scheda di un tema nel diario personale di una persona.
Ricevi lo stato attuale del tema, i punti raccolti finora (numerati) e gli appunti di un nuovo vocale su questo tema. Usa SOLO queste informazioni: niente consigli, niente fatti nuovi.

- sintesi: a che punto è il tema ADESSO, da 1 a 3 frasi brevi, al presente, dando del tu ("stai…", "hai deciso…", "resta da capire…"). Integra le novità con ciò che resta valido; non raccontare i vocali uno per uno.
- superati: i numeri dei punti raccolti finora che il nuovo vocale rende superati: una decisione cambiata, una domanda a cui ora c'è risposta, un'idea abbandonata o sostituita, un fatto non più vero. Lista vuota se nessuno.

Rispondi solo con il JSON."""

SYSTEM_CHIUSURE = """Ricevi alcuni estratti di un vocale e un elenco numerato di cose da fare ancora aperte.
Indica SOLO quelle che negli estratti vengono dette fatte (es. "ho chiamato…", "ho mandato…", "è sistemato") oppure non più necessarie ("non serve più…", "lascio perdere…"). Deve essere detto chiaramente: non dedurlo.

Per ciascuna:
- n: il numero della cosa da fare;
- esito: "fatta" oppure "annullata";
- frase: le parole esatte degli estratti che lo dicono.
Se nessuna, lista vuota. Rispondi solo con il JSON."""


# -- formato delle risposte ----------------------------------------------------------

_VUOTI = ("null", "none", "nessuno", "nessuna", "n/a", "-", "non detto", "non specificato", "")


def _nullable(v: str | None) -> str | None:
    if v is None:
        return None
    v = " ".join(str(v).split())
    return None if v.lower() in _VUOTI else v


def _frase(v: str) -> str:
    v = " ".join(v.split()).strip(" ;-•*")
    return v[:1].upper() + v[1:] if v else v


class DettaglioLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    testo: str
    tipo: str = "punto"
    quando: str | None = None

    @field_validator("tipo", mode="before")
    @classmethod
    def _tipo(cls, v) -> str:
        v = fold(str(v or "punto")).strip().replace(" ", "_").replace("-", "_")
        return v if v in TIPI_PUNTO else ("da_fare" if v in ("dafare", "azione", "todo") else "punto")

    @field_validator("testo")
    @classmethod
    def _testo(cls, v: str) -> str:
        return _frase(v)

    @field_validator("quando")
    @classmethod
    def _quando(cls, v: str | None) -> str | None:
        return _nullable(v)


class PuntoLLM(DettaglioLLM):
    dettagli: list[DettaglioLLM] = Field(default_factory=list)


class BloccoLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    argomento: str
    punti: list[PuntoLLM] = Field(default_factory=list)

    @field_validator("argomento")
    @classmethod
    def _argomento(cls, v: str) -> str:
        v = " ".join(v.strip().strip("\"'«»“”#").split()).rstrip(".")
        if not v:
            raise ValueError("argomento vuoto")
        return v


class RiorganizzaLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    titolo: str = Field(min_length=1, max_length=120)
    categoria: Categoria
    blocchi: list[BloccoLLM] = Field(min_length=1)

    @field_validator("titolo")
    @classmethod
    def _titolo(cls, v: str) -> str:
        v = v.strip().strip("\"'«»“”").rstrip(".").strip()
        if not v:
            raise ValueError("titolo vuoto")
        return v


class TemaLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sintesi: str = Field(min_length=1, max_length=1200)
    superati: list[int] = Field(default_factory=list)

    @field_validator("sintesi")
    @classmethod
    def _sintesi(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("sintesi vuota")
        return v


class ChiusuraLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    n: int
    esito: str = "fatta"
    frase: str = ""


class ChiusureLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fatte: list[ChiusuraLLM] = Field(default_factory=list)


def _voce_schema(con_dettagli: bool) -> dict:
    props: dict = {"testo": {"type": "string"}, "tipo": {"type": "string", "enum": TIPI_PUNTO},
                   "quando": {"type": ["string", "null"]}}
    req = ["testo", "tipo", "quando"]
    if con_dettagli:
        props["dettagli"] = {"type": "array", "maxItems": MAX_DETTAGLI, "items": _voce_schema(False)}
        req.append("dettagli")
    return {"type": "object", "properties": props, "required": req}


RIORGANIZZA_SCHEMA = {
    "type": "object",
    "properties": {
        "titolo": {"type": "string"},
        "categoria": {"type": "string", "enum": CATEGORIE},
        "blocchi": {"type": "array", "minItems": 1, "maxItems": MAX_BLOCCHI, "items": {
            "type": "object",
            "properties": {"argomento": {"type": "string"},
                           "punti": {"type": "array", "minItems": 1, "maxItems": MAX_PUNTI,
                                     "items": _voce_schema(True)}},
            "required": ["argomento", "punti"]}},
    },
    "required": ["titolo", "categoria", "blocchi"],
}

TEMA_SCHEMA = {
    "type": "object",
    "properties": {"sintesi": {"type": "string"},
                   "superati": {"type": "array", "items": {"type": "integer"}}},
    "required": ["sintesi", "superati"],
}

CHIUSURE_SCHEMA = {
    "type": "object",
    "properties": {"fatte": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"},
                                         "esito": {"type": "string", "enum": ["fatta", "annullata"]},
                                         "frase": {"type": "string"}},
        "required": ["n", "esito", "frase"]}}},
    "required": ["fatte"],
}


# -- ingresso e risultato -------------------------------------------------------

@dataclass
class Contesto:
    """Ciò che il modello deve sapere oltre alla trascrizione."""
    # La trascrizione come dialogo ("Voce 1: …"), se parlano più voci.
    dialogo: str | None = None
    voci: set[str] = field(default_factory=set)


@dataclass
class Voce:
    """Un punto (o un dettaglio) degli appunti."""
    tipo: str
    testo: str
    quando: str | None
    fonte: Span | None
    dettagli: list["Voce"] = field(default_factory=list)


@dataclass
class Blocco:
    argomento: str
    punti: list[Voce]


@dataclass
class Uso:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    model: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class AnalysisOutcome:
    titolo: str
    categoria: Categoria
    blocchi: list[Blocco]
    warnings: list[str]
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    parti: int = 1

    def da_fare(self) -> list[tuple[int, Voce]]:
        """Le cose da fare degli appunti, con il blocco a cui appartengono."""
        out = []
        for i, b in enumerate(self.blocchi):
            for p in b.punti:
                for v in [p, *p.dettagli]:
                    if v.tipo == "da_fare":
                        out.append((i, v))
        return out

    def sintesi(self) -> list[str]:
        """Tre frasi per il formato canonico della nota: i primi punti di ogni argomento."""
        righe: list[str] = []
        for k in range(MAX_PUNTI):
            for b in self.blocchi:
                if k < len(b.punti) and len(righe) < 3:
                    p = b.punti[k]
                    righe.append(p.testo if p.testo.endswith((".", "?", "!")) else p.testo + ".")
        while len(righe) < 3:
            righe.append(f"Argomento: {self.blocchi[min(len(righe), len(self.blocchi) - 1)].argomento}."
                         if self.blocchi else "—")
        return righe


@dataclass
class PuntoNoto:
    tipo: str
    testo: str
    quando: datetime | None


@dataclass
class TemaIn:
    """Ciò che serve per aggiornare la scheda di un tema."""
    nome: str
    tipo: str | None
    sintesi: str | None
    punti: list[PuntoNoto]              # raccolti finora, attivi, dal più vecchio
    aperte: list[str]                   # cose da fare aperte del tema
    nota_titolo: str
    nota_data: datetime | None
    nota_appunti: str                   # gli appunti del nuovo vocale su questo tema
    percorso: str | None = None         # i temi che lo contengono: "casa › bagno"


@dataclass
class TemaOut:
    sintesi: str
    superati: list[int]                 # indici (da 0) in TemaIn.punti
    uso: Uso


@dataclass
class Candidata:
    """Una cosa da fare aperta che il vocale potrebbe dire fatta."""
    descrizione: str
    estratto: Span


@dataclass
class Chiusura:
    indice: int
    esito: str
    fonte: Span


_MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def _data(d: datetime | None) -> str:
    return f"{d.day} {_MESI[d.month - 1]} {d.year}" if d else "data sconosciuta"


def _prima_frase(s: str, max_chars: int = 160) -> str:
    s = " ".join(s.split())
    sp = sentences(s)
    t = sp[0].text(s) if sp else s
    return t if len(t) <= max_chars else t[:max_chars].rsplit(" ", 1)[0] + "…"


_ETICHETTA = {"decisione": "deciso", "domanda": "da capire", "idea": "idea", "da_fare": "da fare",
              "fatto": "fatto", "punto": ""}


def testo_voce(v: Voce | DettaglioLLM, rientro: str = "") -> str:
    tag = _ETICHETTA.get(v.tipo, "")
    q = f" (entro: {v.quando})" if getattr(v, "quando", None) else ""
    return f"{rientro}- " + (f"[{tag}] " if tag else "") + v.testo + q


def testo_blocco(b: Blocco) -> str:
    """Il testo di un argomento per gli embedding: il nome e i primi punti."""
    return f"{b.argomento}. " + " ".join(p.testo for p in b.punti[:5])


def testo_blocchi(blocchi: list[Blocco] | list[BloccoLLM], numerati: bool = False) -> str:
    righe = []
    for i, b in enumerate(blocchi, 1):
        righe.append(f"{i}. {b.argomento}" if numerati else f"[{b.argomento}]")
        for p in b.punti:
            righe.append(testo_voce(p, "   "))
            righe.extend(testo_voce(d, "      ") for d in p.dettagli)
    return "\n".join(righe)


# -- analisi ---------------------------------------------------------------------

class Analyzer:
    def __init__(self, client: OllamaClient, model: str, max_context: int = 8192) -> None:
        self.client = client
        self.model = model
        self.max_context = max_context

    def describe(self) -> dict:
        from .colloca import PROMPT_COLLOCA, PROMPT_RIORDINO

        return {"modello": self.model, "contesto_massimo": self.max_context,
                "prompt": f"{PROMPT_VERSION}, {PROMPT_COLLOCA}, {PROMPT_RIORDINO}"}

    def analyze(self, transcript: str, contesto: Contesto | None = None) -> AnalysisOutcome:
        ctx = contesto or Contesto()
        usage = Uso(model=self.model)
        testo = ctx.dialogo or transcript
        budget = max(int((self.max_context - PROMPT_OVERHEAD_TOKENS) * CHARS_PER_TOKEN), 2000)
        parti = 1
        if len(testo) <= budget:
            r = self._riorganizza(SYSTEM, f"Trascrizione:\n<<<\n{testo}\n>>>", usage)
        else:
            parts = _split(testo, budget)
            parti = len(parts)
            appunti = []
            for i, p in enumerate(parts, 1):
                rp = self._riorganizza(SYSTEM_PARTE, f"Parte {i} di {len(parts)}.\n\nTrascrizione:\n<<<\n{p}\n>>>",
                                       usage)
                appunti.append(f"Parte {i} — {rp.titolo}:\n{testo_blocchi(rp.blocchi)}")
            r = self._riorganizza(SYSTEM_UNISCI, "Appunti delle parti, nell'ordine:\n<<<\n" +
                                  "\n\n".join(appunti) + "\n>>>", usage)
            usage.warnings.append(f"vocale lungo: riorganizzato in {len(parts)} parti")

        blocchi = _fonda_blocchi(r.blocchi, transcript, usage.warnings)
        if not blocchi and parti == 1:
            # Niente trova riscontro: si chiede di nuovo, con le parole del vocale.
            usage.warnings.append("nessun appunto trovava riscontro: riorganizzazione richiesta di nuovo")
            r = self._riorganizza(SYSTEM, f"Trascrizione:\n<<<\n{testo}\n>>>\n\nAttenzione: scrivi i punti con "
                                  "le parole del vocale, senza aggiungere niente.", usage)
            blocchi = _fonda_blocchi(r.blocchi, transcript, usage.warnings)
        if not blocchi:
            raise PipelineError("nessun appunto trova riscontro nella trascrizione", code="analisi_non_valida")
        return AnalysisOutcome(
            titolo=r.titolo, categoria=r.categoria, blocchi=blocchi,
            warnings=usage.warnings, model=usage.model, calls=usage.calls, prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens, duration_ms=usage.duration_ms, parti=parti)

    def _riorganizza(self, system: str, msg: str, usage: Uso) -> RiorganizzaLLM:
        def check(r: RiorganizzaLLM) -> str | None:
            problemi = []
            n = title_word_count(r.titolo)
            if not TITOLO_PAROLE[0] <= n <= TITOLO_PAROLE[1]:
                problemi.append(f"il titolo deve avere da 3 a 6 parole, ne ha {n}")
            lunghi = [b.argomento for b in r.blocchi if len(b.argomento.split()) > MAX_PAROLE_ARGOMENTO]
            if lunghi:
                problemi.append("ogni argomento è un nome breve di 1–4 parole, non una frase: "
                                + "; ".join(repr(x) for x in lunghi[:3]))
            fare = [v.testo for b in r.blocchi for p in b.punti for v in [p, *p.dettagli]
                    if v.tipo == "da_fare" and len(v.testo.split()) > MAX_PAROLE_DA_FARE]
            if fare:
                problemi.append("ogni cosa da fare ha al massimo 6 parole, accorcia: "
                                + "; ".join(repr(x) for x in fare[:3]))
            return "; ".join(problemi) or None
        return self._ask(system, msg, RIORGANIZZA_SCHEMA, RiorganizzaLLM, self._ctx_for(msg), usage, check)

    # -- dove va ciò che si dice (vedi colloca.py) -----------------------------------

    def colloca(self, blocchi: list[Blocco], testo_albero: str, nodi: list, indicazioni: list[str]):
        from .colloca import colloca

        usage = Uso(model=self.model)
        return colloca(self, blocchi, testo_albero, nodi, indicazioni, usage), usage

    def riordina(self, testo_albero: str, nodi: list, indicazioni: list[str]):
        from .colloca import riordina

        usage = Uso(model=self.model)
        return riordina(self, testo_albero, nodi, indicazioni, usage), usage

    # -- la scheda di un tema -------------------------------------------------------

    def aggiorna_tema(self, t: TemaIn) -> TemaOut:
        usage = Uso(model=self.model)
        info = ", ".join(x for x in (t.tipo, f"dentro {t.percorso}" if t.percorso else None) if x)
        righe = [f"Tema: {t.nome}" + (f" ({info})" if info else "")]
        righe.append(f"Stato attuale: {t.sintesi}" if t.sintesi else "Stato attuale: tema nuovo, nessuna sintesi.")
        if t.punti:
            righe.append("\nPunti raccolti finora:")
            for i, p in enumerate(t.punti, 1):
                righe.append(f"{i}. [{_ETICHETTA.get(p.tipo) or 'punto'}, {_data(p.quando)}] {p.testo}")
        if t.aperte:
            righe.append("\nCose da fare aperte:")
            righe.extend(f"- {a}" for a in t.aperte)
        righe.append(f"\nNuovo vocale del {_data(t.nota_data)}, «{t.nota_titolo}». Appunti su questo tema:")
        righe.append(t.nota_appunti)
        msg = "\n".join(righe)

        def check(r: TemaLLM) -> str | None:
            fuori = [n for n in r.superati if not 1 <= n <= len(t.punti)]
            if fuori:
                return f"in superati ci sono numeri che non esistono: {fuori}"
            if len(sentences(r.sintesi)) > 4 or len(r.sintesi) > 600:
                return "la sintesi deve essere di 1–3 frasi brevi"
            return None

        r = self._ask(SYSTEM_TEMA, msg, TEMA_SCHEMA, TemaLLM, self._ctx_for(msg), usage, check)
        return TemaOut(r.sintesi, sorted({n - 1 for n in r.superati if 1 <= n <= len(t.punti)}), usage)

    # -- cose fatte -----------------------------------------------------------------

    def chiusure(self, transcript: str, candidate: list[Candidata]) -> tuple[list[Chiusura], Uso]:
        usage = Uso(model=self.model)
        if not candidate:
            return [], usage
        estratti: list[Span] = []
        for c in candidate:
            if not any(c.estratto.start == e.start and c.estratto.end == e.end for e in estratti):
                estratti.append(c.estratto)
        estratti.sort(key=lambda s: s.start)
        msg = ("Estratti del vocale:\n<<<\n" + "\n…\n".join(" ".join(e.text(transcript).split()) for e in estratti)
               + "\n>>>\n\nCose da fare aperte:\n" + "\n".join(f"{i}. {c.descrizione}"
                                                               for i, c in enumerate(candidate, 1)))
        r = self._ask(SYSTEM_CHIUSURE, msg, CHIUSURE_SCHEMA, ChiusureLLM, self._ctx_for(msg), usage)
        out: list[Chiusura] = []
        visti: set[int] = set()
        for f in r.fatte:
            i = f.n - 1
            if not 0 <= i < len(candidate) or i in visti:
                continue
            fonte = _prova_chiusura(f.frase, candidate[i].descrizione, transcript)
            if fonte is None:
                usage.warnings.append(f"chiusura non confermata dal testo, ignorata: {candidate[i].descrizione!r}")
                continue
            visti.add(i)
            out.append(Chiusura(i, "annullata" if f.esito.strip().lower() == "annullata" else "fatta", fonte))
        return out, usage

    # -- chiamate al modello -----------------------------------------------------

    def _ctx_for(self, text: str) -> int:
        need = int(len(text) / CHARS_PER_TOKEN) + PROMPT_OVERHEAD_TOKENS
        ctx = MIN_CONTEXT
        while ctx < need:
            ctx *= 2
        return min(ctx, self.max_context)

    def _ask(self, system: str, user_msg: str, schema: dict, model_cls, num_ctx: int, usage: Uso,
             check: Callable | None = None):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
        last_problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            res = self.client.chat_json(self.model, messages, schema, num_ctx)
            usage.calls += 1
            usage.prompt_tokens += res.prompt_tokens or 0
            usage.output_tokens += res.output_tokens or 0
            usage.duration_ms += res.duration_ms
            usage.model = res.model or usage.model
            try:
                parsed = model_cls.model_validate(json.loads(res.content))
            except json.JSONDecodeError:
                last_problem = "la risposta non è JSON valido"
            except ValidationError as e:
                last_problem = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            else:
                problem = check(parsed) if check else None
                if problem is None or attempt == MAX_ATTEMPTS:
                    if attempt > 1:
                        usage.warnings.append(f"risposta del modello corretta al tentativo {attempt}")
                    if problem:
                        usage.warnings.append(f"accettata con un difetto: {problem}")
                    return parsed
                last_problem = problem
            log.info("risposta da correggere (tentativo %d): %s", attempt, last_problem)
            messages = messages[:2] + [
                {"role": "assistant", "content": res.content},
                {"role": "user", "content": f"La risposta non va bene: {last_problem}. "
                                            "Correggila rispettando tutte le regole e rispondi solo con il JSON."},
            ]
        raise PipelineError(f"risposta del modello non valida dopo {MAX_ATTEMPTS} tentativi: {last_problem}",
                            code="analisi_non_valida")


# -- verifica sulla trascrizione ---------------------------------------------------

def _fonda_voce(v: DettaglioLLM, transcript: str, warnings: list[str]) -> Voce | None:
    if not v.testo:
        return None
    soglia = SUPPORT_DA_FARE if v.tipo == "da_fare" else SUPPORT_PUNTO
    claim = " ".join(x for x in (v.testo, v.quando) if x)
    fonte = best_supporting_sentence(claim, transcript, soglia)
    if fonte is None:
        warnings.append(f"scartato perché non trova riscontro nella trascrizione: {v.testo!r}")
        return None
    quando = v.quando
    if quando and not appears_in(quando, transcript):
        warnings.append(f"scadenza {quando!r} non presente nella trascrizione: rimossa")
        quando = None
    return Voce(v.tipo, v.testo, quando if v.tipo == "da_fare" else None, fonte)


def _fonda_blocchi(raw: list[BloccoLLM], transcript: str, warnings: list[str]) -> list[Blocco]:
    out: list[Blocco] = []
    for b in raw[:MAX_BLOCCHI]:
        punti: list[Voce] = []
        visti: set[str] = set()
        for p in b.punti[:MAX_PUNTI]:
            k = fold(p.testo)
            if k in visti:
                continue
            visti.add(k)
            v = _fonda_voce(p, transcript, warnings)
            if v is None:
                continue
            for d in p.dettagli[:MAX_DETTAGLI]:
                if fold(d.testo) in visti:
                    continue
                dv = _fonda_voce(d, transcript, warnings)
                if dv is not None:
                    visti.add(fold(d.testo))
                    v.dettagli.append(dv)
            punti.append(v)
        if punti:
            # Due blocchi con lo stesso argomento diventano uno.
            gia = next((x for x in out if fold(x.argomento) == fold(b.argomento)), None)
            if gia is not None:
                gia.punti.extend(punti)
            else:
                out.append(Blocco(b.argomento, punti))
    return out


def _prova_chiusura(frase: str, descrizione: str, transcript: str) -> Span | None:
    """La frase che dice fatta una cosa da fare, se è davvero nel testo e parla di quella cosa."""
    if not frase.strip():
        return None
    fonte = best_supporting_sentence(frase, transcript, 0.6)
    if fonte is None:
        return None
    spans = sentences(transcript)
    vicino = fonte
    for i, sp in enumerate(spans):
        if sp.start <= fonte.start < sp.end or (fonte.start <= sp.start < fonte.end):
            vicino = Span(spans[max(i - 1, 0)].start, spans[min(i + 1, len(spans) - 1)].end)
            break
    if not stems(descrizione) & stems(vicino.text(transcript)):
        return None
    return fonte


def _split(text: str, max_chars: int) -> list[str]:
    """Parti di al più max_chars, tagliate a fine frase quando possibile."""
    parts: list[str] = []
    start = 0
    spans = sentences(text)
    cur_end = 0
    for sp in spans:
        if sp.end - start > max_chars and cur_end > start:
            parts.append(text[start:cur_end])
            start = sp.start
        while sp.end - start > max_chars:
            parts.append(text[start:start + max_chars])
            start += max_chars
        cur_end = sp.end
    if cur_end > start:
        parts.append(text[start:cur_end])
    return [p for p in (x.strip() for x in parts) if p]


_TAG_RE = re.compile(r"^[\wàèéìíòóùú][\wàèéìíòóùú' .&/-]{0,39}$", re.IGNORECASE)


def nome_tema(nome: str) -> str | None:
    """Il nome di un tema, ripulito, al massimo 40 caratteri.

    Le maiuscole restano come le scrive il modello (i nomi propri contano: "OpenStreetMap",
    "Valdirame"); solo un nome tutto in maiuscolo di più parole si riporta a lettere normali.
    I nomi si confrontano senza maiuscole, quindi "Bagno" e "bagno" restano lo stesso tema.
    """
    n = " ".join(nome.strip().strip("\"'«»“”#.").split())[:40].strip()
    if not n or not _TAG_RE.match(n):
        return None
    if n.isupper() and len(n.split()) > 1:
        n = n.capitalize()
    return n
