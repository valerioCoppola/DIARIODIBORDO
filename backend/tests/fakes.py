"""Sostituti di faster-whisper e Ollama per i test.

FakeOllama è un server HTTP finto (httpx.MockTransport): il client Ollama vero,
l'analisi, la validazione e il worker girano per intero; cambia solo chi risponde.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import httpx

from app.pipeline import OllamaEmbedder, Pipeline
from app.pipeline.analyze import Analyzer
from app.pipeline.errors import ServiceUnavailable
from app.pipeline.ollama import OllamaClient
from app.pipeline.text import sentences, stems
from app.pipeline.transcribe import Segment, TranscriptResult, assemble

DIMS = 1024


def fake_embedding(text: str, dims: int = DIMS) -> list[float]:
    """Vettore deterministico dalle radici delle parole: testi simili, vettori vicini."""
    v = [0.0] * dims
    for w in stems(text) or {"vuoto"}:
        h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "big")
        v[h % dims] += 1.0
        v[(h >> 20) % dims] += 0.5
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeTranscriber:
    """Il "parlato" è scritto nel file stesso: b"PARLATO:" + testo + rumore casuale."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_next: list[BaseException] = []
        self.glossari: list[str | None] = []

    def describe(self) -> dict:
        return {"modello": "finto", "dispositivo": "cpu"}

    def transcribe(self, path: Path, glossario: str | None = None) -> TranscriptResult:
        self.calls += 1
        self.glossari.append(glossario)
        if self.fail_next:
            raise self.fail_next.pop(0)
        raw = Path(path).read_bytes()
        m = re.match(rb"PARLATO:(.*?)\x00", raw, re.S)
        text = m.group(1).decode() if m else "Nota di prova senza contenuto particolare. Solo una frase. E un'altra."
        segs = []
        t = 0.0
        for sp in sentences(text):
            s = sp.text(text)
            dur = max(1.0, len(s) / 15)
            segs.append(Segment(t, t + dur, " " + s))
            t += dur + 0.4
        joined, out = assemble(segs)
        return TranscriptResult(text=joined, segments=out, language="it", language_probability=0.99,
                                audio_seconds=t, speech_seconds=t * 0.9, model="faster-whisper finto",
                                device="cpu", compute_type="int8", elapsed_ms=5)


def audio_with_speech(text: str) -> bytes:
    import os

    return b"PARLATO:" + text.encode() + b"\x00" + os.urandom(2048)


class FakeOllama:
    def __init__(self) -> None:
        self.down = False
        self.missing_models: set[str] = set()
        self.chat_queue: list[str] = []      # risposte da dare prima di quella automatica
        self.tema_queue: list[str] = []      # idem, per le schede dei temi
        self.chiusure_queue: list[str] = []  # idem, per le cose fatte
        self.colloca_queue: list[str] = []   # idem, per la collocazione nei temi
        self.riordino_queue: list[str] = []  # idem, per il riordino dell'albero
        self.sotto: dict[str, str] = {}      # tema (esistente o nuovo) di cui un argomento è una parte
        self.certezze: dict[str, str] = {}   # quanto il modello è sicuro della collocazione di un argomento
        self.alternative: dict[str, list[str]] = {}
        self.chat_requests: list[dict] = []
        self.embed_requests: list[dict] = []
        self.reject_think = False
        self.bad_answers = False             # risponde sempre con un JSON che non rispetta lo schema
        self.embed_dims = DIMS

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connessione rifiutata", request=request)
        body = json.loads(request.content or b"{}")
        if body.get("model") in self.missing_models:
            return httpx.Response(404, json={"error": f"model \"{body['model']}\" not found, try pulling it first"})
        if request.url.path == "/api/chat":
            if self.reject_think and "think" in body:
                return httpx.Response(400, json={"error": f"\"{body['model']}\" does not support thinking"})
            self.chat_requests.append(body)
            if body.get("stream"):
                testo = self.chat_queue.pop(0) if self.chat_queue else "Ne hai parlato qui [1]."
                pezzi = [testo[i:i + 6] for i in range(0, len(testo), 6)]
                righe = [json.dumps({"model": body["model"], "message": {"role": "assistant", "content": p},
                                     "done": False}) for p in pezzi]
                righe.append(json.dumps({"model": body["model"], "message": {"role": "assistant", "content": ""},
                                         "done": True}))
                return httpx.Response(200, content=("\n".join(righe) + "\n").encode())
            props = (body.get("format") or {}).get("properties", {})
            if "sintesi" in props or "fatte" in props or "collocazione" in props or "modifiche" in props:
                # Collocazione, riordino, schede dei temi e cose fatte: risposte a parte, per non consumare
                # quelle delle note.
                coda = (self.tema_queue if "sintesi" in props else self.chiusure_queue if "fatte" in props
                        else self.colloca_queue if "collocazione" in props else self.riordino_queue)
                content = coda.pop(0) if coda else self.auto_answer(body)
            elif self.chat_queue:
                content = self.chat_queue.pop(0)
                try:   # i test indicano qui il tema di cui un argomento è una parte, e la certezza
                    for b in json.loads(content).get("blocchi", []):
                        if b.get("_sotto"):
                            self.sotto[b["argomento"]] = b["_sotto"]
                        if b.get("_certezza"):
                            self.certezze[b["argomento"]] = b["_certezza"]
                except (ValueError, AttributeError):
                    pass
            elif self.bad_answers:
                content = json.dumps({"titolo": "x"})
            else:
                content = self.auto_answer(body)
            return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", "content": content},
                                             "prompt_eval_count": 100, "eval_count": 50})
        if request.url.path == "/api/embed":
            self.embed_requests.append(body)
            return httpx.Response(200, json={"model": body["model"], "embeddings": [
                fake_embedding(t, self.embed_dims) for t in body["input"]]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "bge-m3:latest"}]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        return httpx.Response(404, json={"error": "percorso sconosciuto"})

    def auto_answer(self, body: dict) -> str:
        """Una risposta plausibile e fedele, costruita dal testo ricevuto."""
        user = body["messages"][1]["content"]
        text = user.split("<<<", 1)[-1].rsplit(">>>", 1)[0].strip()
        props = body["format"]["properties"]
        if "sintesi" in props:
            return _tema(user)
        if "fatte" in props:
            return _chiusure(user)
        if "collocazione" in props:
            return self._colloca(user)
        if "modifiche" in props:
            return json.dumps({"modifiche": []})
        words = re.findall(r"\w+", text)[:5]
        if text.startswith("Parte ") and "\n[" in text:      # unione delle parti di un vocale lungo
            punti = [_da_riga(m.group(1)) for m in re.finditer(r"^   - (.+)$", text, re.M)]
            visti, uniche = set(), []
            for p in punti:
                if p["testo"].lower() not in visti:
                    visti.add(p["testo"].lower())
                    uniche.append(p)
            blocchi = [{"argomento": "progetto Alfa", "punti": uniche[i:i + 7]} for i in range(0, min(len(uniche), 56), 7)]
        else:
            sents = [s.text(text) for s in sentences(text)] or [text]
            scelte = [x for x in sents if re.search(r"\bdevo\b", x, re.I)][:7]
            scelte += [x for x in sents if x not in scelte][:7 - len(scelte)]
            blocchi = [{"argomento": "progetto Alfa", "punti": [_voce(x) for x in sents if x in scelte]}]
        return json.dumps({"titolo": " ".join((words + ["nota", "di", "prova", "oggi"])[:4]),
                           "categoria": "Lavoro", "blocchi": blocchi})

    def _colloca(self, user: str) -> str:
        """Lo stesso nome di un tema esistente è quel tema; altrimenti un tema nuovo con il nome dell'argomento,
        dentro il tema indicato dal test (esistente o nuovo anche lui)."""
        albero, _, argomenti = user.partition("Argomenti del nuovo vocale:")
        esistenti = {m.group(2).strip().lower(): int(m.group(1))
                     for m in re.finditer(r"^\s*(\d+)\. (.+?) \(\d+ not[ae]\)", albero, re.M)}
        out = []
        for m in re.finditer(r"^(\d+)\. (.+)$", argomenti, re.M):
            nome = m.group(2).strip()
            n = esistenti.get(nome.lower())
            if n is not None:
                percorso = [{"tema": n, "nome": None}]
            else:
                padre = self.sotto.get(nome)
                foglia = {"tema": None, "nome": " ".join(nome.split()[:3])}
                if padre and padre.lower() in esistenti:
                    percorso = [{"tema": esistenti[padre.lower()], "nome": None}, foglia]
                elif padre:
                    percorso = [{"tema": None, "nome": padre}, foglia]
                else:
                    percorso = [foglia]
            certezza = self.certezze.get(nome, "alta")
            out.append({"argomento": int(m.group(1)), "percorso": percorso, "certezza": certezza,
                        "domanda": None if certezza == "alta" else f"Dove va «{nome}»?",
                        "alternative": [esistenti[a.lower()] for a in self.alternative.get(nome, [])
                                        if a.lower() in esistenti]})
        return json.dumps({"collocazione": out})


_TAG_TIPO = {"deciso": "decisione", "da capire": "domanda", "idea": "idea", "da fare": "da_fare", "fatto": "fatto"}


def _da_riga(riga: str) -> dict:
    m = re.match(r"\[([^\]]+)\] (.+)$", riga)
    tipo, testo = (_TAG_TIPO.get(m.group(1), "punto"), m.group(2)) if m else ("punto", riga)
    testo = re.sub(r" \(entro: .*\)$", "", testo)
    return {"testo": testo, "tipo": tipo, "quando": None, "dettagli": []}


def _voce(frase: str) -> dict:
    """Un punto da una frase: "devo …" è una cosa da fare, "ho deciso" una decisione, "?" una domanda."""
    f = frase.rstrip(". ")
    m = re.search(r"\bdevo (\w+)(.*)", f, re.I)
    if m:
        return {"testo": (m.group(1) + m.group(2)).rstrip(". "), "tipo": "da_fare", "quando": None, "dettagli": []}
    low = f.lower()
    tipo = ("decisione" if "ho deciso" in low or "abbiamo deciso" in low else "idea" if "idea" in low
            else "domanda" if frase.rstrip().endswith("?") else "fatto")
    return {"testo": f, "tipo": tipo, "quando": None, "dettagli": []}


def _punti(sents: list[str]) -> list[dict]:
    out = []
    for s in sents:
        f = s.lower()
        tipo = ("decisione" if "ho deciso" in f or "abbiamo deciso" in f else "idea" if "idea" in f
                else "domanda" if s.rstrip().endswith("?") else None)
        if tipo:
            out.append({"tipo": tipo, "testo": s.rstrip(".?! "), "tema": "progetto"})
    return out


def _tema(user: str) -> str:
    """Sintesi del tema: i punti nuovi in fila; una decisione nuova supera quelle di prima."""
    nome = re.search(r"^Tema: ([^(\n]+)", user, re.M).group(1).strip()
    nuovi = re.findall(r"^\s*- (?:\[([^\]]+)\] )?(.+)$", user.split("Appunti su questo tema:", 1)[-1], re.M)
    vecchi = re.findall(r"^(\d+)\. \[([^,\]]+), [^\]]*\] (.+)$", user, re.M)
    sintesi = f"Stai seguendo {nome}." + "".join(f" {t.rstrip('.')}." for _, t in nuovi[:2])
    superati = [int(n) for n, tipo, _ in vecchi if tipo == "deciso"] \
        if any(tipo == "deciso" for tipo, _ in nuovi) else []
    return json.dumps({"sintesi": sintesi, "superati": superati})


def _chiusure(user: str) -> str:
    """Una cosa da fare è fatta se negli estratti c'è "ho" + una sua parola."""
    estratti = user.split("<<<", 1)[-1].split(">>>", 1)[0]
    frasi = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n", estratti) if x.strip()]
    fatte = []
    for n, desc in re.findall(r"^(\d+)\. (.+)$", user.split(">>>", 1)[-1], re.M):
        parole = stems(desc)
        for f in frasi:
            if re.search(r"\bho\b", f, re.I) and parole & stems(f):
                fatte.append({"n": int(n), "esito": "fatta", "frase": f})
                break
    return json.dumps({"fatte": fatte})


def _actions(sents: list[str]) -> list[dict]:
    out = []
    for s in sents:
        m = re.search(r"\bdevo (\w+)(.*)", s, re.I)
        if m:
            out.append({"cosa": (m.group(1) + m.group(2)).rstrip(". "), "chi": None, "quando": None})
    return out


class FakeVoci:
    """Riconoscimento finto: alterna le voci frase per frase (o segue turni dati)."""

    def __init__(self, turni: list[tuple[float, float, str]] | None = None, pronto: bool = True) -> None:
        self.turni = turni
        self.pronto = pronto
        self.calls: list[tuple] = []
        self.fail: BaseException | None = None

    def disponibile(self) -> bool:
        return self.pronto

    def describe(self) -> dict:
        return {"pronto": self.pronto, "modello": "finto"}

    def diarizza(self, path, persone=None):
        from app.voci import EsitoVoci

        self.calls.append((path, persone))
        if self.fail:
            raise self.fail
        return EsitoVoci(self.turni if self.turni is not None else self._alterna(path), "cpu", 0.1)

    @staticmethod
    def _alterna(path) -> list[tuple[float, float, str]]:
        """Stessi tempi di FakeTranscriber, una frase per voce, alternando due voci."""
        raw = Path(path).read_bytes()
        m = re.match(rb"PARLATO:(.*?)\x00", raw, re.S)
        text = m.group(1).decode() if m else ""
        out, t = [], 0.0
        for i, sp in enumerate(sentences(text)):
            dur = max(1.0, len(sp.text(text)) / 15)
            out.append((t, t + dur, f"SPEAKER_0{i % 2}"))
            t += dur + 0.4
        return out


def make_pipeline(fake: FakeOllama, transcriber: FakeTranscriber | None = None,
                  max_context: int = 16384, voci: FakeVoci | None = None) -> Pipeline:
    client = OllamaClient("http://ollama.test", transport=fake.transport())
    return Pipeline(transcriber=transcriber or FakeTranscriber(),
                    analyzer=Analyzer(client, "qwen3:8b", max_context),
                    embedder=OllamaEmbedder(client, "bge-m3"), voci=voci)


__all__ = ["FakeOllama", "FakeTranscriber", "FakeVoci", "ServiceUnavailable", "audio_with_speech", "fake_embedding",
           "make_pipeline"]
