"""Client minimo per Ollama, in ascolto solo su localhost.

Usa /api/chat con output strutturato (format = schema JSON) per l'analisi e
/api/embed per gli embedding. Ogni problema di raggiungibilità o un modello
non scaricato diventa ServiceUnavailable: il job aspetta senza perdere tentativi.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import PipelineError, ServiceUnavailable


@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int | None
    output_tokens: int | None
    duration_ms: int


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    model: str
    prompt_tokens: int | None
    duration_ms: int


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 900.0, keep_alive: str | None = "5m",
                 transport: httpx.BaseTransport | None = None, embed_cpu: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        # Embedding sulla CPU: il modello (piccolo) resta nella RAM e non scaccia dalla scheda
        # video quello di analisi a ogni ricerca.
        self.embed_cpu = embed_cpu
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=5.0),
                                  transport=transport)
        self._no_think: set[str] = set()   # modelli che rifiutano il parametro "think"

    def close(self) -> None:
        self._http.close()

    # -- richieste ---------------------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self._http.post(path, json=body)
        except httpx.TimeoutException as e:
            raise PipelineError(f"Ollama non ha risposto in tempo ({path})", code="ollama_timeout") from e
        except httpx.TransportError as e:
            raise ServiceUnavailable(f"Ollama non raggiungibile su {self.base_url}: {type(e).__name__}",
                                     code="ollama_non_raggiungibile") from e
        if r.status_code == 404:
            raise ServiceUnavailable(
                f"modello non installato in Ollama: {body.get('model')} (scaricalo con: ollama pull {body.get('model')})",
                code="modello_non_installato")
        if r.status_code >= 400:
            raise _HttpError(r.status_code, _error_text(r))
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise PipelineError("risposta di Ollama non in JSON", code="ollama_risposta_non_valida") from e

    def _get(self, path: str) -> dict:
        try:
            r = self._http.get(path)
            r.raise_for_status()
            return r.json()
        except httpx.TransportError as e:
            raise ServiceUnavailable(f"Ollama non raggiungibile su {self.base_url}", code="ollama_non_raggiungibile") from e
        except httpx.HTTPStatusError as e:
            raise PipelineError(f"Ollama ha risposto {e.response.status_code} su {path}") from e

    # -- API ---------------------------------------------------------------------

    def version(self) -> str | None:
        return self._get("/api/version").get("version")

    def installed_models(self) -> list[str]:
        return [m.get("name", "") for m in self._get("/api/tags").get("models", [])]

    def load(self, model: str, num_ctx: int, keep_alive: str = "2m") -> None:
        """Carica il modello con il contesto dell'analisi, senza generare niente."""
        self._post_or_fail("/api/generate", {"model": model, "prompt": "", "stream": False,
                                             "keep_alive": keep_alive, "options": {"num_ctx": num_ctx}})

    def unload(self, model: str) -> None:
        try:
            self._post("/api/generate", {"model": model, "prompt": "", "stream": False, "keep_alive": 0})
        except Exception:
            pass

    def loaded(self) -> list[dict]:
        """Modelli in memoria: dimensione totale e parte sulla scheda video (byte)."""
        return self._get("/api/ps").get("models", [])

    def pull(self, model: str) -> None:
        self._http.post("/api/pull", json={"model": model, "stream": False}, timeout=None).raise_for_status()

    def chat_json(self, model: str, messages: list[dict], schema: dict, num_ctx: int,
                  temperature: float = 0.0) -> ChatResult:
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "format": schema,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        # I modelli "a ragionamento" (es. qwen3) qui non devono ragionare a voce alta:
        # serve solo il JSON. Chi non supporta il parametro lo rifiuta: lo si toglie.
        if model not in self._no_think:
            body["think"] = False
        t0 = time.monotonic()
        try:
            data = self._post("/api/chat", body)
        except _HttpError as e:
            if "think" in body and "think" in e.text.lower():
                self._no_think.add(model)
                body.pop("think")
                data = self._post_or_fail("/api/chat", body)
            else:
                raise e.as_pipeline_error() from e
        content = (data.get("message") or {}).get("content") or ""
        return ChatResult(content=content, model=data.get("model", model),
                          prompt_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"),
                          duration_ms=int((time.monotonic() - t0) * 1000))

    def chat_stream(self, model: str, messages: list[dict], num_ctx: int, temperature: float = 0.2):
        """Risposta in testo libero, un pezzo alla volta (per le domande sull'archivio)."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                                "options": {"temperature": temperature, "num_ctx": num_ctx}}
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        if model not in self._no_think:
            body["think"] = False
        for tentativo in (1, 2):
            try:
                with self._http.stream("POST", "/api/chat", json=body) as r:
                    if r.status_code == 404:
                        raise ServiceUnavailable(f"modello non installato in Ollama: {model}",
                                                 code="modello_non_installato")
                    if r.status_code >= 400:
                        testo = r.read().decode("utf-8", "replace")
                        if tentativo == 1 and "think" in body and "think" in testo.lower():
                            self._no_think.add(model)
                            body.pop("think")
                            continue
                        raise PipelineError(f"Ollama ha risposto {r.status_code}", code="ollama_errore")
                    for line in r.iter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise PipelineError(f"Ollama: {data['error']}"[:300], code="ollama_errore")
                        pezzo = (data.get("message") or {}).get("content") or ""
                        if pezzo:
                            yield pezzo
                        if data.get("done"):
                            return
                return
            except httpx.TimeoutException as e:
                raise PipelineError("Ollama non ha risposto in tempo", code="ollama_timeout") from e
            except httpx.TransportError as e:
                raise ServiceUnavailable(f"Ollama non raggiungibile su {self.base_url}",
                                         code="ollama_non_raggiungibile") from e

    def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        body: dict[str, Any] = {"model": model, "input": inputs}
        if self.embed_cpu:
            body["options"] = {"num_gpu": 0}
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        t0 = time.monotonic()
        data = self._post_or_fail("/api/embed", body)
        vectors = data.get("embeddings") or []
        if len(vectors) != len(inputs):
            raise PipelineError(f"Ollama ha restituito {len(vectors)} embedding per {len(inputs)} testi",
                                code="embedding_incompleti")
        return EmbedResult(vectors=vectors, model=data.get("model", model),
                           prompt_tokens=data.get("prompt_eval_count"),
                           duration_ms=int((time.monotonic() - t0) * 1000))

    def _post_or_fail(self, path: str, body: dict) -> dict:
        try:
            return self._post(path, body)
        except _HttpError as e:
            raise e.as_pipeline_error() from e


class _HttpError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}: {text}")
        self.status = status
        self.text = text

    def as_pipeline_error(self) -> PipelineError:
        if self.status >= 500:
            # Es. memoria esaurita nel caricare il modello: può risolversi da sola.
            return PipelineError(f"errore di Ollama ({self.status}): {self.text[:300]}", code="ollama_errore_server")
        return PipelineError(f"richiesta rifiutata da Ollama ({self.status}): {self.text[:300]}",
                             code="ollama_richiesta_rifiutata")


def _error_text(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", r.text))
    except Exception:
        return r.text
