"""Schema canonico della nota (spec. §3), condiviso da pipeline e importazione."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

NOTE_SCHEMA_VERSION = "1"


class Categoria(str, Enum):
    lavoro = "Lavoro"
    idea = "Idea"
    riassunto_giornata = "Riassunto giornata"
    riflessione = "Riflessione"
    personale = "Personale"
    altro = "Altro"


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descrizione: str = Field(min_length=1, max_length=1000)
    responsabile: str | None = Field(default=None, max_length=200)
    scadenza: str | None = Field(default=None, max_length=100)

    @field_validator("descrizione")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("la descrizione non può essere vuota")
        return v

    @field_validator("responsabile", "scadenza")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v if v is None or v.strip() else None


_TAG_RE = re.compile(r"^[\wàèéìíòóùú][\wàèéìíòóùú' -]{0,39}$", re.IGNORECASE)


class Nota(BaseModel):
    """La nota nel formato canonico di esportazione."""

    model_config = ConfigDict(extra="forbid")

    titolo: str = Field(min_length=1, max_length=120)
    data_ora: datetime | None = None
    categoria: Categoria = Categoria.altro
    tag: list[str] = Field(default_factory=list, max_length=5)
    executive_summary: list[str] = Field(min_length=3, max_length=3)
    action_items: list[ActionItem] = Field(default_factory=list)
    trascrizione_grezza: str = Field(min_length=1)

    @field_validator("titolo")
    @classmethod
    def _titolo(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("il titolo non può essere vuoto")
        return v

    @field_validator("data_ora")
    @classmethod
    def _offset_obbligatorio(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.utcoffset() is None:
            raise ValueError("data_ora deve essere ISO 8601 con offset (es. 2026-10-21T08:30:00+02:00)")
        return v

    @field_validator("tag")
    @classmethod
    def _tag(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip()
            if not _TAG_RE.match(t):
                raise ValueError(f"tag non valido: {t!r} (max 40 caratteri, lettere, cifre, spazi, -)")
            if t.lower() not in (x.lower() for x in out):
                out.append(t)
        return out

    @field_validator("executive_summary")
    @classmethod
    def _summary(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("le tre frasi di executive_summary non possono essere vuote")
        return v

    @field_validator("trascrizione_grezza")
    @classmethod
    def _trascrizione(cls, v: str) -> str:
        # Nessuna normalizzazione: il testo è conservato esattamente com'è.
        if not v.strip():
            raise ValueError("la trascrizione non può essere vuota")
        return v


def title_word_count(titolo: str) -> int:
    return len(titolo.split())


def soft_warnings(nota: Nota) -> list[str]:
    """Regole della specifica che non rendono la nota invalida ma vanno segnalate."""
    warnings: list[str] = []
    n = title_word_count(nota.titolo)
    if not 3 <= n <= 7:
        warnings.append(f"il titolo ha {n} parole, la specifica ne prevede 3–6")
    return warnings


def json_schema() -> dict:
    schema = Nota.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Diario di Bordo — Nota"
    return schema
