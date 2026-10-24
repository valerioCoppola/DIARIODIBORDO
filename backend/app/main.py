"""Applicazione FastAPI del Diario di Bordo."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_settings
from .db import close_pool, get_pool
from .routers import admin, archive, auth, chiarimenti, chiedi, diari, entries, grafo, system, uploads
from .storage import ensure_storage


class NoContentLogFilter(logging.Filter):
    """Le righe di accesso riportano metodo, percorso e stato: niente query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and "?" in args[2]:
                args[2] = args[2].split("?", 1)[0] + "?…"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(NoContentLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level)
    ensure_storage()
    get_pool()
    yield
    close_pool()


app = FastAPI(
    title="Diario di Bordo",
    version=__version__,
    description="API private per l'acquisizione e la consultazione del Diario di Bordo.",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
# La pagina della documentazione API usa script da CDN: CSP più permissiva solo lì.
CSP_APP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
           "worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if not request.url.path.startswith("/api/docs"):
        response.headers.setdefault("Content-Security-Policy", CSP_APP)
    path = request.url.path
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith("/_next/static/"):
        # Nomi con l'impronta del contenuto: non cambiano mai, si tengono per sempre.
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif path.startswith(("/fonts/", "/icons/")):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    else:
        # Pagine, sw.js, manifest: sempre ricontrollate. Senza, il browser può tenersi
        # l'HTML di una versione precedente, che punta a file che non ci sono più
        # (pagina bianca, senza stile, dopo un aggiornamento).
        response.headers.setdefault("Cache-Control", "no-cache")
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"errore": "errore_http", "messaggio": exc.detail}
    return JSONResponse(detail, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = [{"campo": ".".join(str(p) for p in e["loc"]), "messaggio": e["msg"]} for e in exc.errors()]
    return JSONResponse({"errore": "richiesta_non_valida", "messaggio": "Dati non validi", "dettagli": errors},
                        status_code=422)


for r in (system.router, auth.router, diari.router, admin.router, uploads.router, archive.router, entries.router,
          grafo.router, chiedi.router, chiarimenti.router):
    app.include_router(r)


# ---------------------------------------------------------------------------
# Frontend statico (build Next.js). Stessa origine delle API: niente CORS.
# ---------------------------------------------------------------------------

_dist = get_settings().frontend_dist
if _dist.is_dir():
    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(_dist / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    # Icone che i browser chiedono da soli alla radice del sito.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(_dist / "icons" / "icon-192.png", media_type="image/png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def apple_touch_icon():
        return FileResponse(_dist / "icons" / "apple-touch-icon.png", media_type="image/png")

    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    def no_frontend():
        return JSONResponse({"messaggio": "Frontend non compilato: eseguire `npm run build` in frontend/. "
                                          "Documentazione API su /api/docs"})
