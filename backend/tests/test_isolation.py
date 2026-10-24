"""Isolamento fra utenti: API e, sotto, Row Level Security nel database."""

import os
import uuid

import psycopg
import pytest

CHUNK = 64 * 1024


def _upload(client) -> dict:
    data = os.urandom(CHUNK)
    up = client.post("/api/v1/uploads", headers={"Idempotency-Key": f"iso-{uuid.uuid4()}"},
                     json={"mime_type": "audio/webm", "total_size": len(data), "chunk_size": CHUNK}).json()
    client.put(f"/api/v1/uploads/{up['id']}/chunks/0", content=data)
    return client.post(f"/api/v1/uploads/{up['id']}/complete").json()


def test_other_user_cannot_see_or_download(alice, bruno):
    done = _upload(alice)
    rid, jid = done["recording_id"], done["job_id"]
    assert bruno.get(f"/api/v1/recordings/{rid}").status_code == 404
    assert bruno.get(f"/api/v1/recordings/{rid}/audio").status_code == 404
    assert bruno.get(f"/api/v1/jobs/{jid}").status_code == 404
    assert all(r["id"] != rid for r in bruno.get("/api/v1/recordings").json()["elementi"])
    # Nemmeno sulla sessione di caricamento altrui
    assert bruno.get(f"/api/v1/uploads/{done['id']}").status_code == 404


def test_same_file_for_two_users_is_not_shared(alice, bruno):
    data = os.urandom(CHUNK)
    ids = []
    for client in (alice, bruno):
        r = client.post("/api/v1/recordings", files={"file": ("a.webm", data, "audio/webm")})
        assert r.status_code == 201 and r.json()["duplicato"] is False
        ids.append(r.json()["recording_id"])
    assert ids[0] != ids[1]


def test_rls_enforced_in_database(alice, bruno):
    """Anche con SQL diretto come ruolo applicativo, si vedono solo le proprie righe."""
    _upload(alice)
    url = os.environ["DATABASE_URL"]
    a_id = alice.get("/api/v1/auth/me").json()["id"]
    b_id = bruno.get("/api/v1/auth/me").json()["id"]
    with psycopg.connect(url) as conn:
        # Senza utente impostato: nessuna riga
        assert conn.execute("SELECT count(*) FROM recordings").fetchone()[0] == 0
        conn.execute("SELECT set_config('app.user_id', %s, true)", (b_id,))
        owners = {r[0] for r in conn.execute("SELECT owner_id FROM recordings").fetchall()}
        assert str(a_id) not in {str(o) for o in owners}
        conn.rollback()

        # Inserire righe a nome di un altro utente è vietato
        conn.execute("SELECT set_config('app.user_id', %s, true)", (b_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO ingestion_jobs (owner_id, kind, idempotency_key) VALUES (%s, 'audio', 'x-forged')",
                (a_id,))
        conn.rollback()


def test_app_role_cannot_bypass_rls(admin_conn):
    row = admin_conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'diario_app'").fetchone()
    assert row == (False, False)
    forced = admin_conn.execute(
        "SELECT count(*) FROM pg_class WHERE relname IN ('recordings','entries','text_chunks','ingestion_jobs') "
        "AND relrowsecurity AND relforcerowsecurity").fetchone()[0]
    assert forced == 4
