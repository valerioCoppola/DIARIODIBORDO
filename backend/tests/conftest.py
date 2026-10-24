"""Fixture: database di test reale (Postgres + pgvector), migrazioni, due utenti.

Richiede TEST_DATABASE_ADMIN_URL verso un server Postgres su cui l'utente può
creare database, es.:
    TEST_DATABASE_ADMIN_URL=postgresql://diario:...@127.0.0.1:5432/postgres
Il database `diario_test_<random>` viene creato e poi eliminato.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL")
if not ADMIN_URL:
    pytest.exit("Impostare TEST_DATABASE_ADMIN_URL (vedi tests/conftest.py)", returncode=2)

DB_NAME = f"diario_test_{secrets.token_hex(4)}"
# Il ruolo diario_app è di tutto il server Postgres: cambiargli password scollegherebbe
# il Diario in funzione sullo stesso server. scripts/test.sh esporta quella di .env.
APP_PASSWORD = os.environ.get("DB_APP_PASSWORD") or secrets.token_urlsafe(16)
TMP = Path(tempfile.mkdtemp(prefix="diario-test-"))


def _with_db(url: str, db: str, user: str | None = None, password: str | None = None) -> str:
    p = urlsplit(url)
    netloc = p.netloc
    if user:
        host = netloc.split("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((p.scheme, netloc, "/" + db, p.query, p.fragment))


with psycopg.connect(ADMIN_URL, autocommit=True) as c:
    c.execute(f'CREATE DATABASE "{DB_NAME}"')

os.environ.update({
    "DATABASE_ADMIN_URL": _with_db(ADMIN_URL, DB_NAME),
    "DATABASE_URL": _with_db(ADMIN_URL, DB_NAME, "diario_app", APP_PASSWORD),
    "DB_APP_PASSWORD": APP_PASSWORD,
    "STORAGE_DIR": str(TMP / "audio"),
    "UPLOAD_TMP_DIR": str(TMP / "uploads"),
    "COOKIE_SECURE": "false",
    "FRONTEND_DIST": str(TMP / "nofrontend"),
    "DEFAULT_CHUNK_BYTES": str(64 * 1024),
})

from app.db import close_pool, run_migrations  # noqa: E402

run_migrations()

PASSWORD_A = "password-utente-a-123"
PASSWORD_B = "password-utente-b-456"


@pytest.fixture(scope="session", autouse=True)
def _teardown():
    yield
    close_pool()
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def users():
    from app.cli import main
    main(["create-user", "alice", "--password", PASSWORD_A])
    main(["create-user", "bruno", "--password", PASSWORD_B])
    return {"alice": PASSWORD_A, "bruno": PASSWORD_B}


@pytest.fixture(scope="session")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


def _login(app, username: str, password: str):
    from fastapi.testclient import TestClient
    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    # Si entra nel primo diario (quello creato con l'account), come toccarlo sullo scaffale.
    primo = min(client.get("/api/v1/diari").json()["diari"], key=lambda d: d["creato_il"])
    assert client.post(f"/api/v1/diari/{primo['id']}/apri").status_code == 200
    return client


@pytest.fixture
def alice(app, users):
    c = _login(app, "alice", users["alice"])
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def bruno(app, users):
    c = _login(app, "bruno", users["bruno"])
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def anon(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_conn():
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        yield c
