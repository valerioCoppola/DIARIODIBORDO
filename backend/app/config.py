"""Configurazione da variabili d'ambiente (vedi .env.example nella radice)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # Connessione usata a runtime: ruolo diario_app, soggetto a RLS.
    database_url: str = "postgresql://diario_app:change-me@127.0.0.1:5432/diario"
    # Connessione amministrativa usata solo dalle migrazioni.
    database_admin_url: str = "postgresql://diario:change-me@127.0.0.1:5432/diario"
    db_app_password: str = "change-me"

    # Directory privata dei file audio. Mai servita come statico.
    storage_dir: Path = PROJECT_ROOT / "data" / "audio"
    upload_tmp_dir: Path = PROJECT_ROOT / "data" / "tmp" / "uploads"

    # Caricamento
    max_upload_bytes: int = 4 * 1024**3          # 4 GiB per file
    default_chunk_bytes: int = 8 * 1024**2       # 8 MiB
    max_chunk_bytes: int = 32 * 1024**2          # 32 MiB
    upload_session_ttl_hours: int = 24 * 7
    allowed_audio_mime_prefixes: tuple[str, ...] = ("audio/", "video/webm", "video/mp4")

    # Sessioni e sicurezza
    session_ttl_days: int = 30
    cookie_secure: bool = True                   # False solo in sviluppo su http://localhost
    session_cookie_name: str = "ddb_session"
    login_max_failures: int = 8
    login_lockout_minutes: int = 15

    # Frontend statico (build di Next.js con output: export)
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "out"

    # Rete: per impostazione predefinita solo localhost; l'accesso dal telefono
    # passa da un proxy HTTPS (Tailscale Serve), vedi docs/accesso-dal-telefono.md
    host: str = "127.0.0.1"
    port: int = 8000

    log_level: str = Field(default="INFO")

    # --- Elaborazione (Incremento 2): tutto in locale ---------------------------
    # Trascrizione con faster-whisper. "auto" sceglie in base alla GPU (vedi
    # pipeline/transcribe.py): large-v3 se la memoria video basta, altrimenti
    # large-v3-turbo; senza GPU large-v3-turbo su CPU.
    whisper_model: str = "auto"
    whisper_device: str = "auto"                 # auto | cuda | cpu
    whisper_compute_type: str = "auto"           # auto | float16 | int8_float16 | int8
    whisper_language: str = "it"                 # "auto" per riconoscerla
    whisper_beam_size: int = 5
    # Glossario per il riconoscimento: nomi delle voci e dei temi vengono
    # suggeriti a Whisper (hotwords), così i nomi propri ricorrenti si scrivono bene.
    whisper_glossario: bool = True
    # Tenere il modello in memoria video tra un vocale e l'altro. "auto": sì se
    # la GPU ha almeno 16 GB, così resta spazio per il modello di analisi.
    whisper_keep_loaded: str = "auto"

    # Analisi e embedding con Ollama, solo su localhost.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:9b"   # scelto da `cli scegli-modello`
    ollama_embed_model: str = "bge-m3"
    ollama_timeout_seconds: float = 900.0
    ollama_keep_alive: str = "5m"
    # Contesto massimo per l'analisi (token). Oltre, la trascrizione viene
    # analizzata a parti e poi riassunta (vedi pipeline/analyze.py). 8192 tiene
    # un modello da 8–9 miliardi tutto in una scheda video da 8 GB.
    analysis_max_context: int = 8192
    embedding_dimensions: int = 1024             # deve coincidere con vector(1024) nello schema
    # bge-m3 sulla CPU (circa 1,2 GB di RAM): la scheda video resta al modello di analisi.
    embedding_su_cpu: bool = True
    # Temi: uno nuovo confluisce in uno esistente allo stesso livello se gli embedding
    # dei due nomi sono almeno così simili (coseno).
    topic_merge_similarity: float = 0.86
    # Controlli sulla collocazione proposta dal modello (coseno tra l'argomento e ciò che
    # il tema raccoglie). Sotto la soglia di coerenza, o con un altro tema chiaramente più
    # vicino, la collocazione diventa incerta e, se c'è posto, una domanda.
    collocazione_coerenza: float = 0.40          # tema con una scheda
    collocazione_coerenza_nome: float = 0.30     # tema conosciuto solo per nome
    collocazione_alternativa: float = 0.55       # un altro tema così vicino è un'alternativa…
    collocazione_margine: float = 0.10           # …se lo è almeno di tanto più di quello scelto
    collocazione_dimenticato: float = 0.62       # un tema nuovo così vicino a uno esistente: chiedere
    nome_simile: float = 0.80                    # nomi così simili (embedding): "è lo stesso?"
    domande_per_vocale: int = 2
    # Riordino dell'albero: ogni tante note collocate, o quando la coda si svuota se
    # l'ultimo riordino è più vecchio di riordino_ore.
    riordino_ogni: int = 5
    riordino_ore: float = 12.0

    # Riconoscimento delle voci (pyannote, in un ambiente Python a parte creato da
    # scripts/prepara-ai.sh). Se non è preparato, le note restano senza voci.
    voci_python: Path = PROJECT_ROOT / ".venv-voci" / "bin" / "python"
    voci_modello: Path = PROJECT_ROOT / "data" / "modelli" / "pyannote-community-1"
    voci_timeout_seconds: int = 7200
    # Ricerca per significato: oltre questa distanza coseno un segmento non è
    # considerato pertinente (bge-m3: testi sullo stesso tema stanno sotto 0,5).
    search_max_distance: float = 0.55

    # Worker
    worker_poll_seconds: float = 10.0
    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: float = 20.0
    worker_max_backoff_seconds: int = 3600
    # Servizio temporaneamente non disponibile (es. Ollama spento): riprova senza
    # consumare tentativi.
    worker_unavailable_retry_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
