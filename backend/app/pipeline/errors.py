"""Errori della pipeline, distinti per come il worker deve reagire."""

from __future__ import annotations


class PipelineError(Exception):
    """Errore ritentabile: il job torna in coda con attesa crescente."""

    code = "errore_elaborazione"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ServiceUnavailable(PipelineError):
    """Un servizio locale non risponde (es. Ollama spento, modello non scaricato).

    Il job riprova a breve senza consumare tentativi: appena il servizio torna
    disponibile l'elaborazione riparte da sola.
    """

    code = "servizio_non_disponibile"


class PermanentError(PipelineError):
    """Errore che un nuovo tentativo non risolve (es. audio senza parlato)."""

    code = "errore_definitivo"
