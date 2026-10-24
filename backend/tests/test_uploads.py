import hashlib
import os
import uuid
from pathlib import Path

CHUNK = 64 * 1024


def _audio(n: int) -> bytes:
    return os.urandom(n)


def _init(client, data: bytes, key: str | None = None, **extra):
    body = {"mime_type": "audio/webm;codecs=opus", "total_size": len(data), "chunk_size": CHUNK,
            "filename": "vocale.webm", **extra}
    return client.post("/api/v1/uploads", headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"}, json=body)


def _put(client, uid, i, data: bytes):
    part = data[i * CHUNK:(i + 1) * CHUNK]
    return client.put(f"/api/v1/uploads/{uid}/chunks/{i}", content=part,
                      headers={"X-Chunk-SHA256": hashlib.sha256(part).hexdigest(),
                               "Content-Type": "application/octet-stream"})


def test_chunked_upload_full_flow(alice, admin_conn):
    data = _audio(CHUNK * 3 + 1234)
    r = _init(alice, data, sha256=hashlib.sha256(data).hexdigest(), source="registratore",
              recorded_at="2026-10-01T08:30:00+02:00", recorded_at_source="registratore_integrato",
              duration_seconds=1800.5)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["numero_blocchi"] == 4 and up["blocchi_mancanti"] == [0, 1, 2, 3]

    for i in (2, 0, 3, 1):  # ordine qualsiasi
        assert _put(alice, up["id"], i, data).status_code == 200

    r = alice.post(f"/api/v1/uploads/{up['id']}/complete")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["stato"] == "completata" and done["duplicato"] is False
    assert done["job_status"] == "originale_conservato"

    rec = alice.get(f"/api/v1/recordings/{done['recording_id']}").json()
    assert rec["sha256"] == hashlib.sha256(data).hexdigest()
    assert rec["dimensione"] == len(data) and rec["formato"] == "webm/opus"
    assert rec["durata_secondi"] == 1800.5 and rec["fonte_data"] == "registratore_integrato"
    assert rec["registrato_il"] == "2026-10-01T08:30:00+02:00"
    assert rec["job"]["stato"] == "originale_conservato"

    # L'originale è identico byte per byte e scaricabile solo autenticati
    audio = alice.get(f"/api/v1/recordings/{done['recording_id']}/audio")
    assert audio.status_code == 200 and audio.content == data

    # Offset orario conservato
    off = admin_conn.execute("SELECT recorded_utc_offset_minutes FROM recordings WHERE id = %s",
                             (done["recording_id"],)).fetchone()[0]
    assert off == 120

    # Stato persistente con storico eventi
    job = alice.get(f"/api/v1/jobs/{done['job_id']}").json()
    assert [e["a"] for e in job["eventi"]] == ["ricevuto", "originale_conservato"]
    assert job["concluso"] is False  # caricato ≠ archiviato


def test_files_are_private_on_disk(alice):
    data = _audio(CHUNK + 10)
    up = _init(alice, data).json()
    for i in range(up["numero_blocchi"]):
        _put(alice, up["id"], i, data)
    done = alice.post(f"/api/v1/uploads/{up['id']}/complete").json()
    from app.db import user_tx
    from app import storage
    me = alice.get("/api/v1/auth/me").json()
    with user_tx(me["id"], me["diario"]["id"]) as c:
        rel = c.execute("SELECT storage_path FROM recordings WHERE id = %s", (done["recording_id"],)).fetchone()
    path = storage.absolute_path(rel["storage_path"])
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert not storage.upload_dir(uuid.UUID(up["id"])).exists()  # blocchi temporanei rimossi


def test_resume_after_interruption(alice):
    data = _audio(CHUNK * 5)
    key = f"resume-{uuid.uuid4()}"
    up = _init(alice, data, key=key).json()
    _put(alice, up["id"], 0, data)
    _put(alice, up["id"], 3, data)
    # "Connessione persa": il client ripete l'init con la stessa chiave
    r = _init(alice, data, key=key)
    assert r.status_code == 200
    resumed = r.json()
    assert resumed["id"] == up["id"] and resumed["blocchi_mancanti"] == [1, 2, 4]
    # Completare prima del tempo è rifiutato e indica cosa manca
    r = alice.post(f"/api/v1/uploads/{up['id']}/complete")
    assert r.status_code == 409 and r.json()["blocchi_mancanti"] == [1, 2, 4]
    for i in resumed["blocchi_mancanti"]:
        _put(alice, up["id"], i, data)
    assert alice.post(f"/api/v1/uploads/{up['id']}/complete").json()["stato"] == "completata"


def test_repeated_operations_do_not_duplicate(alice, admin_conn):
    data = _audio(CHUNK * 2)
    key = f"rep-{uuid.uuid4()}"
    up = _init(alice, data, key=key).json()
    for _ in range(2):
        for i in range(2):
            assert _put(alice, up["id"], i, data).status_code == 200
    first = alice.post(f"/api/v1/uploads/{up['id']}/complete").json()
    second = alice.post(f"/api/v1/uploads/{up['id']}/complete").json()
    assert first["recording_id"] == second["recording_id"] and first["job_id"] == second["job_id"]
    # blocco ripetuto dopo il completamento: innocuo
    assert _put(alice, up["id"], 0, data).status_code == 200
    # stesso contenuto, nuova chiave: riconosciuto come duplicato
    up2 = _init(alice, data).json()
    for i in range(2):
        _put(alice, up2["id"], i, data)
    dup = alice.post(f"/api/v1/uploads/{up2['id']}/complete").json()
    assert dup["duplicato"] is True and dup["recording_id"] == first["recording_id"]
    sha = hashlib.sha256(data).digest()
    assert admin_conn.execute("SELECT count(*) FROM recordings WHERE sha256 = %s", (sha,)).fetchone()[0] == 1
    assert admin_conn.execute("SELECT count(*) FROM ingestion_jobs WHERE recording_id = %s",
                              (first["recording_id"],)).fetchone()[0] == 1


def test_known_hash_skips_upload(alice):
    data = _audio(CHUNK)
    up = _init(alice, data).json()
    _put(alice, up["id"], 0, data)
    first = alice.post(f"/api/v1/uploads/{up['id']}/complete").json()
    r = _init(alice, data, sha256=hashlib.sha256(data).hexdigest())
    assert r.status_code == 201
    assert r.json()["stato"] == "completata" and r.json()["duplicato"] is True
    assert r.json()["recording_id"] == first["recording_id"]


def test_same_key_different_params_rejected(alice):
    key = f"conf-{uuid.uuid4()}"
    assert _init(alice, _audio(CHUNK), key=key).status_code == 201
    r = _init(alice, _audio(CHUNK * 2), key=key)
    assert r.status_code == 409 and r.json()["errore"] == "chiave_riutilizzata"


def test_hash_mismatch_resets_chunks(alice):
    data = _audio(CHUNK * 2)
    up = _init(alice, data, sha256="ab" * 32).json()
    for i in range(2):
        _put(alice, up["id"], i, data)
    r = alice.post(f"/api/v1/uploads/{up['id']}/complete")
    assert r.status_code == 422 and r.json()["errore"] == "impronta_non_corrispondente"
    assert alice.get(f"/api/v1/uploads/{up['id']}").json()["blocchi_mancanti"] == [0, 1]


def test_chunk_validation(alice):
    data = _audio(CHUNK * 2 + 5)
    up = _init(alice, data).json()
    r = alice.put(f"/api/v1/uploads/{up['id']}/chunks/0", content=data[:100])
    assert r.status_code == 422 and r.json()["errore"] == "lunghezza_blocco_errata"
    r = alice.put(f"/api/v1/uploads/{up['id']}/chunks/7", content=data[:CHUNK])
    assert r.status_code == 422 and r.json()["errore"] == "indice_non_valido"
    r = alice.put(f"/api/v1/uploads/{up['id']}/chunks/0", content=data[:CHUNK],
                  headers={"X-Chunk-SHA256": "00" * 32})
    assert r.status_code == 422 and r.json()["errore"] == "impronta_blocco_errata"
    assert _put(alice, up["id"], 2, data).status_code == 200  # ultimo blocco corto


def test_init_validation(alice):
    assert _init(alice, b"x" * 10, mime_type="text/plain").status_code == 415
    r = _init(alice, b"x" * 10, recorded_at="2026-10-21T08:30:00")
    assert r.status_code == 422 and r.json()["errore"] == "data_senza_offset"
    r = _init(alice, b"x" * 10, recorded_at="2026-10-21T08:30:00+02:00")
    assert r.status_code == 422 and r.json()["errore"] == "fonte_data_mancante"
    r = alice.post("/api/v1/uploads", headers={"Idempotency-Key": "corta"},
                   json={"mime_type": "audio/webm", "total_size": 10})
    assert r.status_code == 422
    r = alice.post("/api/v1/uploads", json={"mime_type": "audio/webm", "total_size": 10})
    assert r.status_code == 422  # Idempotency-Key obbligatoria


def test_cancel_upload(alice):
    data = _audio(CHUNK * 2)
    up = _init(alice, data).json()
    _put(alice, up["id"], 0, data)
    assert alice.delete(f"/api/v1/uploads/{up['id']}").json()["stato"] == "annullata"
    assert _put(alice, up["id"], 1, data).status_code == 409


def test_single_request_upload(alice):
    data = _audio(5000)
    files = {"file": ("nota.m4a", data, "audio/mp4")}
    r = alice.post("/api/v1/recordings", files=files, data={"source": "caricamento"})
    assert r.status_code == 201, r.text
    first = r.json()
    r = alice.post("/api/v1/recordings", files=files)
    assert r.status_code == 200 and r.json()["duplicato"] is True
    assert r.json()["recording_id"] == first["recording_id"]
    r = alice.post("/api/v1/recordings", files={"file": ("x.txt", b"ciao", "text/plain")})
    assert r.status_code == 415


def test_status_summary(alice):
    s = alice.get("/api/v1/status").json()
    assert s["registrazioni"] >= 1 and s["job_per_stato"]["originale_conservato"] >= 1
