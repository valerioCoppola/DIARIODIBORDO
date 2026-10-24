"""Segmentazione della trascrizione per l'indicizzazione.

Segmenti di circa TARGET caratteri, tagliati a fine frase, con una frase di
sovrapposizione tra un segmento e il successivo: una domanda che cade a cavallo
del taglio trova comunque un segmento che la contiene. Ogni segmento conserva la
posizione esatta nel testo originale (content == testo[char_start:char_end]).
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import Span, sentences

TARGET_CHARS = 900
MAX_CHARS = 1600


@dataclass
class Chunk:
    ord: int
    char_start: int
    char_end: int
    content: str


def _split_long(span: Span) -> list[Span]:
    """Una "frase" troppo lunga (trascrizione senza punteggiatura) va spezzata sugli spazi."""
    out: list[Span] = []
    a = span.start
    while span.end - a > MAX_CHARS:
        out.append(Span(a, a + TARGET_CHARS))
        a += TARGET_CHARS
    out.append(Span(a, span.end))
    return out


def chunk_text(text: str) -> list[Chunk]:
    units: list[Span] = []
    for s in sentences(text):
        units.extend(_split_long(s) if s.end - s.start > MAX_CHARS else [s])
    units = [_trim(text, u) for u in units]
    units = [u for u in units if u.end > u.start]
    chunks: list[Chunk] = []
    i = 0
    while i < len(units):
        j = i
        size = units[i].end - units[i].start
        while j + 1 < len(units) and size < TARGET_CHARS and units[j + 1].end - units[i].start <= MAX_CHARS:
            j += 1
            size = units[j].end - units[i].start
        a, b = units[i].start, units[j].end
        chunks.append(Chunk(len(chunks), a, b, text[a:b]))
        if j + 1 >= len(units):
            break
        # Sovrapposizione di una frase, purché si avanzi sempre.
        i = j if j > i else j + 1
    return chunks


def _trim(text: str, sp: Span) -> Span:
    a, b = sp.start, sp.end
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return Span(a, b)
