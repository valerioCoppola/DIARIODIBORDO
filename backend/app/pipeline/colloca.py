"""Dove va ciò che si dice: collocazione degli argomenti e riordino dell'albero dei temi.

L'archivio è un albero che nasce solo da ciò che la persona racconta: nessuna categoria
decisa in anticipo. In cima stanno i temi più ampi emersi finora, sotto le loro parti.
Tre regole tengono l'albero fedele:

  1. un tema è una cosa che tornerà (un progetto, un'organizzazione, un luogo, una persona,
     un interesse, una questione), mai un'attività o un compito: quelli sono appunti
     dentro il tema a cui servono;
  2. ogni argomento si colloca con un PERCORSO dal tema più ampio al più specifico
     (non con un solo nome): così anche il primo vocale su qualcosa fa nascere la cosa
     ampia, e non solo il dettaglio di cui si parlava quel giorno;
  3. il modello dice quanto è sicuro, e propone una domanda quando non lo è. I controlli
     che non dipendono da lui (vedi collocazione.py) possono abbassare la sua certezza.

Il riordino guarda l'albero intero ogni tanto: raggruppa sotto un tema nuovo più temi che
si rivelano parti di una stessa cosa, sposta, unisce i doppioni, rinomina i temi che hanno
il nome di un'attività. È ciò che permette all'albero di correggersi crescendo, invece di
cristallizzare la prima collocazione.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .text import fold

if TYPE_CHECKING:
    from .analyze import Analyzer, Blocco, Uso

PROMPT_COLLOCA = "colloca-2"
PROMPT_RIORDINO = "riordino-1"
MAX_PASSI = 4
MAX_PAROLE_NOME = 4            # nel prompt 3
MAX_ALBERO = 140               # temi mostrati al modello
MAX_MODIFICHE = 6
CERTEZZE = ["alta", "media", "bassa"]
TIPI_MODIFICA = ["raggruppa", "sposta", "unisci", "rinomina"]

# Nomi che dicono che genere di cosa è, non quale: non sono temi.
GENERICI = {fold(x) for x in (
    "progetti", "progetto", "idee", "idea", "attività", "attivita", "lavori", "cose da fare", "da fare", "compiti",
    "task", "varie", "vario", "generale", "altro", "altri", "argomenti", "argomento", "temi", "tema", "note",
    "appunti", "aggiornamenti", "questioni", "cose", "misc", "miscellanea", "personale", "riflessioni")}

_REGOLE_TEMA = """Un tema è qualcosa di cui la persona tornerà a parlare: un progetto, un'iniziativa, un'organizzazione, un prodotto, un luogo, una persona o un gruppo, un interesse, una questione che dura nel tempo. Un'attività singola o un compito (telefonare, aggiornare, mandare, comprare, sistemare…) NON è un tema: sta negli appunti del tema a cui serve.
In cima all'albero stanno i temi più ampi; ogni sotto-tema è una parte del tema che lo contiene (una parte, una fase, un luogo, un filone di lavoro). Non esistono categorie decise in anticipo: i temi e il modo in cui si raggruppano nascono solo da ciò che la persona dice.
Nomi: da 1 a 3 parole, come li chiama la persona (nomi propri compresi), mai nomi di categorie ("progetti", "idee", "attività", "lavori", "cose da fare", "varie", "generale", "altro"): il nome della cosa stessa."""

SYSTEM_COLLOCA = f"""Ordini l'archivio di un "secondo cervello": un albero di temi che cresce con ciò che una persona racconta nei suoi vocali.
{_REGOLE_TEMA}

Ricevi l'albero dei temi esistenti, numerati (i sotto-temi sono rientrati sotto il tema che li contiene; tra parentesi quante note ne parlano; per alcuni c'è a che punto sono), le indicazioni date dalla persona e gli argomenti di un nuovo vocale, numerati, con i loro appunti.

Per ogni argomento:
- argomento: il suo numero;
- percorso: la strada nell'albero dal tema più ampio a quello più specifico a cui l'argomento appartiene, da 1 a {MAX_PASSI} passi. Ogni passo è {{"tema": numero di un tema esistente, "nome": null}} oppure {{"tema": null, "nome": nome di un tema nuovo}}.
  - Il primo passo è il tema più ampio che ciò che è detto, o l'albero, permette di riconoscere. Se l'argomento è parte di qualcosa di più grande e il vocale lo fa capire, quella cosa più grande è il primo passo, anche se è nuova.
  - Un passo esistente deve stare, nell'albero, dentro il passo esistente che lo precede.
  - Un tema esistente che sta in cima all'albero può venire dopo un passo nuovo solo se il vocale mostra che è una parte di quella cosa nuova: allora finisce dentro di essa.
  - L'ultimo passo è la cosa più specifica che tornerà, mai l'attività in sé.
  - Usa un tema esistente quando è la stessa cosa, anche detta con altre parole. Non mettere una cosa dentro un tema esistente solo perché è vicina: deve esserne davvero una parte.
- certezza: "alta" se il vocale dice chiaramente di cosa fa parte, o è evidente che è un tema esistente; "media" se è probabile; "bassa" se stai indovinando o manca un'informazione;
- domanda: se la certezza non è alta, una domanda breve da fare alla persona per esserne sicuri (es. "«il preventivo» è per la casa di via Roma?"); altrimenti null;
- alternative: fino a 2 numeri di temi esistenti dove potrebbe andare invece; altrimenti [].
Le indicazioni date dalla persona valgono più di tutto il resto.

Rispondi solo con il JSON."""

SYSTEM_RIORDINO = f"""Curi l'albero dei temi di un "secondo cervello" che cresce con i vocali di una persona.
{_REGOLE_TEMA}

Ricevi l'albero (numerato; i sotto-temi rientrati sotto il loro tema; tra parentesi quante note; per alcuni a che punto è; "fissato" vuol dire messo lì dalla persona: non va toccato) e le indicazioni date dalla persona.
Proponi solo le modifiche che rendono l'albero chiaramente più fedele a ciò che è stato raccontato:
- "raggruppa": due o più temi allo stesso livello che sono parti di una stessa cosa più ampia, che nell'albero manca → un tema nuovo che li contiene (nome: quella cosa più ampia);
- "sposta": un tema che è una parte di un altro tema esistente ma sta altrove → dentro quel tema (dentro: il suo numero); oppure in cima (dentro: null) se non è una parte del tema in cui sta;
- "unisci": due temi che sono la stessa cosa → il primo confluisce nel secondo (dentro: il numero del secondo);
- "rinomina": un tema che ha il nome di un'attività o di una categoria invece che della cosa → il nome della cosa.
Per ciascuna: tipo; temi (i numeri dei temi coinvolti: almeno 2 per "raggruppa", 1 per le altre); dentro (per "sposta" e "unisci", altrimenti null); nome (per "raggruppa" e "rinomina", altrimenti null); motivo (una frase breve).
Al massimo {MAX_MODIFICHE} modifiche. Se l'albero va bene così, nessuna modifica: una modifica superflua è peggio di nessuna.

Rispondi solo con il JSON."""


# -- l'albero come lo vede il modello -------------------------------------------------------

@dataclass(eq=False)
class Nodo:
    """Un tema dell'archivio."""
    id: Any
    nome: str
    note: int = 0
    sintesi: str | None = None
    fissato: bool = False
    alias: set[str] = field(default_factory=set)      # slug e altri nomi con cui è stato chiamato
    padre: "Nodo | None" = None
    figli: list["Nodo"] = field(default_factory=list)
    numero: int = 0                    # nel testo mostrato al modello

    def antenati(self) -> list["Nodo"]:
        out, x = [], self.padre
        while x is not None:
            out.insert(0, x)
            x = x.padre
        return out

    def cammino(self) -> list["Nodo"]:
        return [*self.antenati(), self]

    def contiene(self, altro: "Nodo") -> bool:
        """Vero se altro è questo nodo o sta dentro di esso."""
        x: Nodo | None = altro
        while x is not None:
            if x is self:
                return True
            x = x.padre
        return False


def _prima_frase(s: str, max_chars: int = 140) -> str:
    s = " ".join(s.split())
    for sep in (". ", "? ", "! "):
        if sep in s:
            s = s.split(sep, 1)[0] + sep.strip()
            break
    return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + "…"


def albero_testo(radici: list[Nodo], vicini: set[Any] | None = None, tutte_le_sintesi: bool = False,
                 limite: int = MAX_ALBERO) -> tuple[str, list[Nodo]]:
    """L'albero numerato, e i temi nell'ordine dei numeri.

    Se i temi sono troppi si mostrano quelli vicini a ciò di cui si parla (con chi li
    contiene) e, per il resto, i livelli più alti: il modello vede sempre la forma generale.
    """
    if not radici:
        return "L'archivio è ancora vuoto: nessun tema.", []
    vicini = vicini or set()
    tutti: list[Nodo] = []
    coda = list(radici)
    while coda:                                   # in ampiezza: prima i livelli alti
        n = coda.pop(0)
        tutti.append(n)
        coda.extend(n.figli)
    scelti: set[int] = set()
    for n in tutti:
        if n.id in vicini:
            scelti.update(id(x) for x in n.cammino())
    for n in tutti:
        if len(scelti) >= limite:
            break
        if n.padre is None or id(n.padre) in scelti:
            scelti.add(id(n))
    righe = ["Albero dei temi esistenti:"]
    numerati: list[Nodo] = []

    def scrivi(n: Nodo, prof: int) -> None:
        if id(n) not in scelti:
            return
        numerati.append(n)
        n.numero = len(numerati)
        info = [f"{n.note} {'nota' if n.note == 1 else 'note'}"]
        riga = f"{'   ' * prof}{n.numero}. {n.nome} ({', '.join(info)})"
        if n.fissato:
            riga += " — fissato"
        if n.sintesi and (tutte_le_sintesi or n.id in vicini):
            riga += f" — {_prima_frase(n.sintesi)}"
        righe.append(riga)
        nascosti = sum(1 for f in n.figli if id(f) not in scelti)
        for f in n.figli:
            scrivi(f, prof + 1)
        if nascosti:
            righe.append(f"{'   ' * (prof + 1)}(e altri {nascosti} sotto-temi)")

    for r in radici:
        scrivi(r, 0)
    return "\n".join(righe), numerati


def indicazioni_testo(indicazioni: list[str]) -> str:
    if not indicazioni:
        return "Indicazioni date dalla persona: nessuna."
    return "Indicazioni date dalla persona:\n" + "\n".join(f"- {x}" for x in indicazioni)


# -- risposte del modello -------------------------------------------------------------------

def _vuoto(v: Any) -> str | None:
    if v is None:
        return None
    v = " ".join(str(v).split())
    return None if fold(v) in ("", "null", "none", "nessuno", "nessuna", "-", "n/a") else v


class PassoLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tema: int | None = None
    nome: str | None = None

    @field_validator("nome", mode="before")
    @classmethod
    def _nome(cls, v):
        return _vuoto(v)


class CollocaVoceLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    argomento: int
    percorso: list[PassoLLM] = Field(default_factory=list)
    certezza: str = "media"
    domanda: str | None = None
    alternative: list[int] = Field(default_factory=list)

    @field_validator("certezza", mode="before")
    @classmethod
    def _certezza(cls, v):
        v = fold(str(v or ""))
        return v if v in CERTEZZE else "media"

    @field_validator("domanda", mode="before")
    @classmethod
    def _domanda(cls, v):
        return _vuoto(v)

    @field_validator("alternative", mode="before")
    @classmethod
    def _alternative(cls, v):
        return [x for x in (v or []) if isinstance(x, int)]


class CollocaLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    collocazione: list[CollocaVoceLLM] = Field(default_factory=list)


class ModificaLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tipo: str
    temi: list[int] = Field(default_factory=list)
    dentro: int | None = None
    nome: str | None = None
    motivo: str = ""

    @field_validator("tipo", mode="before")
    @classmethod
    def _tipo(cls, v):
        return fold(str(v or ""))

    @field_validator("nome", mode="before")
    @classmethod
    def _nome(cls, v):
        return _vuoto(v)


class RiordinoLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    modifiche: list[ModificaLLM] = Field(default_factory=list)


_PASSO_SCHEMA = {"type": "object", "properties": {"tema": {"type": ["integer", "null"]},
                                                  "nome": {"type": ["string", "null"]}},
                 "required": ["tema", "nome"]}

COLLOCA_SCHEMA = {
    "type": "object",
    "properties": {"collocazione": {"type": "array", "items": {
        "type": "object",
        "properties": {"argomento": {"type": "integer"},
                       "percorso": {"type": "array", "minItems": 1, "maxItems": MAX_PASSI, "items": _PASSO_SCHEMA},
                       "certezza": {"type": "string", "enum": CERTEZZE},
                       "domanda": {"type": ["string", "null"]},
                       "alternative": {"type": "array", "maxItems": 2, "items": {"type": "integer"}}},
        "required": ["argomento", "percorso", "certezza", "domanda", "alternative"]}}},
    "required": ["collocazione"],
}

RIORDINO_SCHEMA = {
    "type": "object",
    "properties": {"modifiche": {"type": "array", "maxItems": MAX_MODIFICHE, "items": {
        "type": "object",
        "properties": {"tipo": {"type": "string", "enum": TIPI_MODIFICA},
                       "temi": {"type": "array", "items": {"type": "integer"}},
                       "dentro": {"type": ["integer", "null"]},
                       "nome": {"type": ["string", "null"]},
                       "motivo": {"type": "string"}},
        "required": ["tipo", "temi", "dentro", "nome", "motivo"]}}},
    "required": ["modifiche"],
}


# -- risultato ------------------------------------------------------------------------------

@dataclass
class Passo:
    nodo: Nodo | None = None           # un tema esistente
    nome: str | None = None            # oppure uno nuovo

    def etichetta(self) -> str:
        return self.nodo.nome if self.nodo is not None else (self.nome or "")


@dataclass
class Proposta:
    percorso: list[Passo]
    certezza: str = "alta"
    domanda: str | None = None
    alternative: list[Nodo] = field(default_factory=list)


@dataclass
class Modifica:
    tipo: str
    temi: list[Nodo]
    dentro: Nodo | None = None
    nome: str | None = None
    motivo: str = ""


def nome_valido(nome: str | None) -> str | None:
    """Il nome di un tema nuovo, ripulito; None se non è un nome di tema accettabile."""
    from .analyze import nome_tema

    n = nome_tema(nome or "")
    if not n or len(n.split()) > MAX_PAROLE_NOME or fold(n) in GENERICI:
        return None
    return n


def _problema_percorso(c: CollocaVoceLLM, nodi: list[Nodo]) -> str | None:
    if not c.percorso:
        return f"argomento {c.argomento}: il percorso è vuoto"
    if len(c.percorso) > MAX_PASSI:
        return f"argomento {c.argomento}: al massimo {MAX_PASSI} passi"
    ultimo: Nodo | None = None
    stato = "inizio"            # → "esistenti", "nuovi_in_cima" (nuovi prima di ogni esistente), "nuovi"
    for p in c.percorso:
        if p.tema is not None:
            if not 1 <= p.tema <= len(nodi):
                return f"argomento {c.argomento}: il tema {p.tema} non esiste"
            n = nodi[p.tema - 1]
            if stato == "nuovi":
                return (f"argomento {c.argomento}: dopo un tema nuovo dentro un tema esistente non può venire "
                        f"un altro tema esistente ({p.tema}, {n.nome})")
            if stato == "nuovi_in_cima" and n.padre is not None:
                return (f"argomento {c.argomento}: dopo un tema nuovo può venire solo un tema che sta in cima "
                        f"all'albero, e {p.tema} ({n.nome}) sta dentro {n.padre.nome}")
            if ultimo is not None and not ultimo.contiene(n):
                return (f"argomento {c.argomento}: il tema {p.tema} ({n.nome}) non sta dentro "
                        f"{ultimo.numero} ({ultimo.nome})")
            ultimo, stato = n, "esistenti"
        else:
            if not p.nome:
                return f"argomento {c.argomento}: ogni passo ha un tema esistente oppure il nome di uno nuovo"
            if len(p.nome.split()) > MAX_PAROLE_NOME:
                return f"argomento {c.argomento}: il nome {p.nome!r} deve avere da 1 a 3 parole"
            if fold(p.nome) in GENERICI:
                return (f"argomento {c.argomento}: {p.nome!r} è una categoria, non una cosa: usa il nome della "
                        "cosa stessa")
            stato = "nuovi" if stato == "esistenti" else ("nuovi_in_cima" if stato == "inizio" else stato)
    return None


def _normalizza(c: CollocaVoceLLM, nodi: list[Nodo], warnings: list[str]) -> Proposta:
    """Percorso completo: i temi esistenti con chi li contiene, i nuovi con un nome valido.

    Un passo che non rispetta le regole (scampato ai tentativi di correzione) si lascia cadere.
    """
    passi: list[Passo] = []
    ultimo: Nodo | None = None
    stato = "inizio"
    for p in c.percorso[:MAX_PASSI]:
        if p.tema is not None and 1 <= p.tema <= len(nodi):
            n = nodi[p.tema - 1]
            if stato == "inizio":
                passi.extend(Passo(x) for x in n.antenati())
            elif stato == "esistenti" and ultimo is not None and ultimo.contiene(n):
                passi.extend(Passo(x) for x in n.antenati() if x is not ultimo and ultimo.contiene(x))
            elif not (stato == "nuovi_in_cima" and n.padre is None):
                warnings.append(f"collocazione: tema {n.nome!r} fuori posto nel percorso, ignorato")
                continue
            passi.append(Passo(n))
            ultimo, stato = n, "esistenti"
        else:
            nome = nome_valido(p.nome)
            if nome is None:
                warnings.append(f"collocazione: nome di tema non valido scartato: {p.nome!r}")
                continue
            if any(x.nodo is None and fold(x.nome or "") == fold(nome) for x in passi):
                continue
            passi.append(Passo(nome=nome))
            stato = "nuovi" if stato == "esistenti" else ("nuovi_in_cima" if stato == "inizio" else stato)
    alt = [nodi[a - 1] for a in c.alternative if 1 <= a <= len(nodi)]
    return Proposta(passi, c.certezza, c.domanda, alt[:2])


def colloca(an: "Analyzer", blocchi: list["Blocco"], testo_albero: str, nodi: list[Nodo],
            indicazioni: list[str], usage: "Uso") -> list[Proposta]:
    """Un percorso per ogni argomento del vocale, con quanto il modello ne è sicuro."""
    from .analyze import testo_blocchi

    msg = (f"{testo_albero}\n\n{indicazioni_testo(indicazioni)}\n\n"
           f"Argomenti del nuovo vocale:\n{testo_blocchi(blocchi, numerati=True)}")

    def check(r: CollocaLLM) -> str | None:
        visti = [c.argomento for c in r.collocazione]
        mancano = [i for i in range(1, len(blocchi) + 1) if i not in visti]
        if mancano:
            return f"manca la collocazione degli argomenti {mancano}"
        for c in r.collocazione:
            if 1 <= c.argomento <= len(blocchi):
                p = _problema_percorso(c, nodi)
                if p:
                    return p
        return None

    r = an._ask(SYSTEM_COLLOCA, msg, COLLOCA_SCHEMA, CollocaLLM, an._ctx_for(msg), usage, check)
    per: dict[int, CollocaVoceLLM] = {}
    for c in r.collocazione:
        per.setdefault(c.argomento, c)
    out: list[Proposta] = []
    for i, b in enumerate(blocchi, 1):
        c = per.get(i)
        prop = _normalizza(c, nodi, usage.warnings) if c is not None else None
        if prop is None or not prop.percorso:
            # Niente di usabile: l'argomento stesso, come tema a sé da confermare.
            nome = nome_valido(" ".join(b.argomento.split()[:3]))
            prop = Proposta([Passo(nome=nome)] if nome else [], "bassa", None, [])
            usage.warnings.append(f"collocazione non valida per {b.argomento!r}: da confermare")
        out.append(prop)
    return out


def riordina(an: "Analyzer", testo_albero: str, nodi: list[Nodo], indicazioni: list[str],
             usage: "Uso") -> list[Modifica]:
    """Le modifiche che rendono l'albero più fedele; quelle impossibili si scartano."""
    msg = f"{testo_albero}\n\n{indicazioni_testo(indicazioni)}"
    r = an._ask(SYSTEM_RIORDINO, msg, RIORDINO_SCHEMA, RiordinoLLM, an._ctx_for(msg), usage)
    out: list[Modifica] = []
    toccati: set[int] = set()
    for m in r.modifiche[:MAX_MODIFICHE]:
        motivo = " ".join(m.motivo.split())[:200]
        temi = [nodi[t - 1] for t in m.temi if 1 <= t <= len(nodi)]
        dentro = nodi[m.dentro - 1] if m.dentro is not None and 1 <= m.dentro <= len(nodi) else None
        scarta = None
        if m.tipo not in TIPI_MODIFICA or not temi or len(temi) != len(set(map(id, temi))):
            scarta = "modifica non valida"
        elif any(t.fissato for t in temi):
            scarta = "tocca un tema fissato dalla persona"
        elif any(id(t) in toccati for t in temi):
            scarta = "tocca un tema già modificato"
        elif m.tipo == "raggruppa":
            nome = nome_valido(m.nome)
            if len(temi) < 2 or len({id(t.padre) for t in temi}) != 1:
                scarta = "per raggruppare servono due o più temi allo stesso livello"
            elif nome is None:
                scarta = f"nome non valido {m.nome!r}"
            elif any(fold(f.nome) == fold(nome) for f in (temi[0].padre.figli if temi[0].padre else [])):
                scarta = f"esiste già {nome!r} a quel livello"
            else:
                out.append(Modifica("raggruppa", temi, None, nome, motivo))
        elif len(temi) != 1:
            scarta = "una sola modifica per tema"
        elif m.tipo == "sposta":
            t = temi[0]
            if dentro is not None and t.contiene(dentro):
                scarta = "non si può spostare un tema dentro se stesso"
            elif dentro is t.padre:
                scarta = "è già lì"
            else:
                out.append(Modifica("sposta", temi, dentro, None, motivo))
        elif m.tipo == "unisci":
            t = temi[0]
            if dentro is None or t.contiene(dentro) or dentro.contiene(t):
                scarta = "si uniscono solo due temi distinti, nessuno dentro l'altro"
            else:
                out.append(Modifica("unisci", temi, dentro, None, motivo))
        elif m.tipo == "rinomina":
            nome = nome_valido(m.nome)
            if nome is None or fold(nome) == fold(temi[0].nome):
                scarta = f"nome non valido {m.nome!r}"
            else:
                out.append(Modifica("rinomina", temi, None, nome, motivo))
        if scarta:
            usage.warnings.append(f"riordino: scartata {m.tipo} {[t.nome for t in temi]}: {scarta}")
        else:
            toccati.update(id(t) for t in temi)
            if dentro is not None:
                toccati.add(id(dentro))
    return out
