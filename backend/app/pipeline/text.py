"""Strumenti sul testo: frasi con posizione, normalizzazione, ricerca della fonte."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Fine frase: . ! ? … seguiti da spazio/a capo, oppure un a capo doppio.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n\s*\n")
_WORD = re.compile(r"[\wàèéìíòóùú]+", re.IGNORECASE)   # "l'orto" → "l", "orto"

# Parole troppo comuni per dire se un'informazione è nel testo.
_STOP = set("""
a ad al alla alle allo agli ai anche c che chi ci con cosa così da dal dalla dalle dei del della delle dello
degli di e ed è era fa fare gli ha hai ho i il in io la le li lo ma mi ne nei nel nella nelle no non o per
però più poi qua quale quando quello questa questo quindi se si sì sia siamo sono su sua sue suo sul sulla
ti tra tu tutto un una uno va vi devo deve dobbiamo bisogna fatto fare
""".split())


@dataclass
class Span:
    start: int
    end: int

    def text(self, src: str) -> str:
        return src[self.start:self.end]


def sentences(text: str) -> list[Span]:
    """Frasi con la loro posizione esatta nel testo (spazi esclusi ai bordi)."""
    spans: list[Span] = []
    pos = 0
    for m in _SENTENCE_END.finditer(text):
        _add(spans, text, pos, m.start())
        pos = m.end()
    _add(spans, text, pos, len(text))
    return spans


def _add(spans: list[Span], text: str, a: int, b: int) -> None:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    if b > a:
        spans.append(Span(a, b))


def fold(s: str) -> str:
    """Minuscole senza accenti: per confronti tolleranti."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def content_words(s: str) -> set[str]:
    return {w for w in (fold(x) for x in _WORD.findall(s)) if len(w) > 2 and w not in _STOP}


def stems(s: str) -> set[str]:
    """Radici grossolane: "chiamare", "chiamo" e "chiamato" condividono "chiam"."""
    return {w[:5] if len(w) > 5 else w for w in content_words(s)}


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")
    return s[:60].strip("-")


def appears_in(needle: str, haystack: str) -> bool:
    """Vero se tutte le parole significative di needle compaiono nel testo."""
    words = content_words(needle) or {fold(needle).strip()}
    hay = content_words(haystack)
    hay_text = fold(haystack)
    return all(w in hay or w in hay_text for w in words if w)


def best_supporting_sentence(claim: str, text: str, min_overlap: float = 0.5) -> Span | None:
    """La frase della trascrizione che meglio giustifica un'affermazione.

    Restituisce None se nessuna frase condivide almeno min_overlap delle parole
    significative dell'affermazione: in quel caso l'affermazione non ha una fonte
    riconoscibile nel testo.
    """
    words = stems(claim)
    if not words:
        return None
    best: tuple[float, Span] | None = None
    spans = sentences(text)
    # Anche coppie di frasi consecutive: un'azione detta a cavallo di due frasi.
    candidates = spans + [Span(a.start, b.end) for a, b in zip(spans, spans[1:])]
    for sp in candidates:
        ov = len(words & stems(sp.text(text))) / len(words)
        if best is None or ov > best[0] + 1e-9 or (abs(ov - best[0]) < 1e-9 and sp.end - sp.start < best[1].end - best[1].start):
            best = (ov, sp)
    if best is None or best[0] < min_overlap:
        return None
    return best[1]
