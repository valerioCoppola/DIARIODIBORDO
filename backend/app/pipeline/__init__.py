"""Pipeline di elaborazione: trascrizione, analisi, indicizzazione.

I componenti sono sostituibili (nei test si usano versioni finte):
  transcriber.transcribe(path, glossario) -> TranscriptResult
  analyzer.analyze(testo, contesto)  -> AnalysisOutcome
  embedder.embed(testi)              -> (vettori, modello, durata_ms)
  voci.diarizza(path, persone)       -> EsitoVoci (facoltativo: chi parla)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import Settings, get_settings
from .analyze import Analyzer, AnalysisOutcome
from .ollama import OllamaClient
from .transcribe import FasterWhisperTranscriber, TranscriptResult

PIPELINE_VERSION = "5.0"


class Transcriber(Protocol):
    def transcribe(self, path, glossario: str | None = None) -> TranscriptResult: ...
    def describe(self) -> dict: ...


class AnalyzerLike(Protocol):
    def analyze(self, transcript: str, contesto=None) -> AnalysisOutcome: ...
    def describe(self) -> dict: ...


class Voci(Protocol):
    def disponibile(self) -> bool: ...
    def diarizza(self, path, persone: int | None = None): ...
    def describe(self) -> dict: ...


class Embedder(Protocol):
    model: str
    def embed(self, texts: list[str]) -> tuple[list[list[float]], str, int]: ...


class OllamaEmbedder:
    def __init__(self, client: OllamaClient, model: str, batch: int = 16) -> None:
        self.client = client
        self.model = model
        self.batch = batch

    def embed(self, texts: list[str]) -> tuple[list[list[float]], str, int]:
        vectors: list[list[float]] = []
        ms = 0
        model = self.model
        for i in range(0, len(texts), self.batch):
            r = self.client.embed(self.model, texts[i:i + self.batch])
            vectors.extend(r.vectors)
            ms += r.duration_ms
            model = r.model or model
        return vectors, model, ms


@dataclass
class Pipeline:
    transcriber: Transcriber
    analyzer: AnalyzerLike
    embedder: Embedder
    voci: Voci | None = None

    def describe(self) -> dict:
        return {"trascrizione": self.transcriber.describe(), "analisi": self.analyzer.describe(),
                "embedding": {"modello": self.embedder.model},
                "voci": self.voci.describe() if self.voci else {"pronto": False}, "versione": PIPELINE_VERSION}


def build_pipeline(settings: Settings | None = None) -> Pipeline:
    from ..voci import Diarizzatore

    s = settings or get_settings()
    client = OllamaClient(s.ollama_url, timeout=s.ollama_timeout_seconds, keep_alive=s.ollama_keep_alive,
                          embed_cpu=s.embedding_su_cpu)
    return Pipeline(
        transcriber=FasterWhisperTranscriber(s),
        analyzer=Analyzer(client, s.ollama_model, s.analysis_max_context),
        embedder=OllamaEmbedder(client, s.ollama_embed_model),
        voci=Diarizzatore(s),
    )
