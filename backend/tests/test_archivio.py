"""Archivio: eliminazione completa, file spariti dal PC, copia d'ascolto, calendario."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from app import storage
from app.playback import playback_path
from app.worker import Worker

from fakes import FakeOllama, FakeTranscriber, audio_with_speech, make_pipeline

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="serve ffmpeg")


@pytest.fixture
def fake():
    return FakeOllama()


@pytest.fixture
def worker(fake):
    w = Worker(make_pipeline(fake, FakeTranscriber()), worker_id="test-archivio")
    drain(w)
    return w


def drain(w: Worker) -> None:
    for _ in range(100):
        if not w.run_once():
            return
    raise AssertionError("il worker non si ferma")


def real_audio(tmp_path: Path, kind: str = "webm", seconds: int = 3) -> bytes:
    """Audio come lo produce il registratore del browser: WebM "live" senza durata, o MP4 frammentato."""
    f = tmp_path / f"r.{kind}"
    args = ["-c:a", "libopus", "-f", "webm", "-live", "1"] if kind == "webm" else \
           ["-c:a", "aac", "-movflags", "frag_keyframe+empty_moov"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={random.randint(150, 900)}:duration={seconds}",
                    "-ac", "1", "-b:a", "48k", *args, str(f)], check=True)
    return f.read_bytes()


def upload(client, data: bytes, mime="audio/webm;codecs=opus", recorded_at=None, name="vocale.webm") -> dict:
    form = {"source": "registratore"}
    if recorded_at:
        form |= {"recorded_at": recorded_at, "recorded_at_source": "registratore_integrato"}
    r = client.post("/api/v1/recordings", files={"file": (name, data, mime)}, data=form)
    assert r.status_code == 201, r.text
    return r.json()


def path_of(admin_conn, recording_id) -> Path:
    rel = admin_conn.execute("SELECT storage_path FROM recordings WHERE id = %s", (recording_id,)).fetchone()[0]
    return storage.absolute_path(rel)


def listed(client, recording_id) -> bool:
    return any(x["id"] == recording_id for x in client.get("/api/v1/recordings?limit=200").json()["elementi"])


# -- eliminazione ----------------------------------------------------------------------------

def test_delete_removes_recording_and_everything_derived(alice, bruno, worker, fake, admin_conn, tmp_path):
    fake.chat_queue = [json.dumps({
        "executive_summary": ["Uno.", "Due.", "Tre."], "titolo": "Nota da eliminare del tutto",
        "categoria": "Idea", "argomenti": [{"nome": "argomento-solo-qui", "area": "Varie"}],
        "azioni": [{"cosa": "provare la nota di prova", "chi": None, "quando": None}]})]
    data = real_audio(tmp_path)
    up = upload(alice, data)
    drain(worker)
    rid, jid = up["recording_id"], up["job_id"]
    entry = admin_conn.execute("SELECT id FROM entries WHERE recording_id = %s", (rid,)).fetchone()[0]
    original = path_of(admin_conn, rid)
    assert original.is_file() and playback_path(original).is_file()   # copia d'ascolto preparata dal worker

    assert bruno.delete(f"/api/v1/recordings/{rid}").status_code == 404  # di Alice: per Bruno non esiste
    r = alice.delete(f"/api/v1/recordings/{rid}")
    assert r.status_code == 200 and r.json()["eliminato"] is True

    q = lambda sql, *a: admin_conn.execute(sql, a).fetchone()[0]       # noqa: E731
    assert q("SELECT count(*) FROM recordings WHERE id = %s", rid) == 0
    assert q("SELECT count(*) FROM entries WHERE id = %s", entry) == 0
    for table in ("entry_versions", "text_chunks", "action_items", "entry_topics", "transcriptions"):
        assert q(f"SELECT count(*) FROM {table} WHERE entry_id = %s", entry) == 0, table
    assert q("SELECT count(*) FROM ingestion_jobs WHERE id = %s", jid) == 0
    assert q("SELECT count(*) FROM job_events WHERE job_id = %s", jid) == 0
    assert q("SELECT count(*) FROM model_usage WHERE job_id = %s", jid) == 0
    assert q("SELECT count(*) FROM topics WHERE slug = 'argomento-solo-qui'") == 0   # argomento rimasto orfano
    assert not original.exists() and not playback_path(original).exists()
    assert not listed(alice, rid)
    assert alice.get(f"/api/v1/entries/{entry}").status_code == 404
    assert alice.delete(f"/api/v1/recordings/{rid}").status_code == 404

    # Lo stesso audio si può caricare di nuovo: non risulta più un duplicato
    assert alice.post("/api/v1/recordings", files={"file": ("x.webm", data, "audio/webm")}).status_code == 201


def test_shared_topics_survive_deletion(alice, worker, fake, admin_conn):
    from test_conoscenza import risposta

    for _ in range(2):
        fake.chat_queue.append(risposta(titolo="Due note stesso tema", argomenti=[("tema condiviso", "Varie")]))
    a = upload(alice, audio_with_speech("Prima nota sul tema. Una frase. Due."))
    upload(alice, audio_with_speech("Seconda nota sul tema. Una frase. Due."))
    drain(worker)
    assert alice.delete(f"/api/v1/recordings/{a['recording_id']}").status_code == 200
    assert admin_conn.execute("SELECT count(*) FROM topics WHERE slug = 'tema-condiviso'").fetchone()[0] == 1


def test_delete_from_note(alice, worker, admin_conn):
    up = upload(alice, audio_with_speech("Una nota da togliere dalla sua pagina. Ecco. Fine."))
    drain(worker)
    entry = admin_conn.execute("SELECT id FROM entries WHERE recording_id = %s", (up["recording_id"],)).fetchone()[0]
    assert alice.delete(f"/api/v1/entries/{entry}").status_code == 200
    assert admin_conn.execute("SELECT count(*) FROM recordings WHERE id = %s",
                              (up["recording_id"],)).fetchone()[0] == 0


def test_delete_while_worker_is_processing(alice, worker, admin_conn):
    up = upload(alice, audio_with_speech("Eliminata mentre il PC ci lavora. Pazienza. Fine."))
    claimed = worker.claim()
    assert claimed and str(claimed[0]) == str(up["job_id"])
    assert alice.delete(f"/api/v1/recordings/{up['recording_id']}").status_code == 200
    worker.process(*claimed)                      # non deve rompersi né ricreare nulla
    assert admin_conn.execute("SELECT count(*) FROM entries WHERE recording_id = %s",
                              (up["recording_id"],)).fetchone()[0] == 0


# -- file spariti dal PC ----------------------------------------------------------------------

def test_missing_original_is_ignored_and_comes_back(alice, admin_conn, tmp_path):
    up = upload(alice, audio_with_speech("Questo file sparirà dal PC. Poi torna. Fine."))
    p = path_of(admin_conn, up["recording_id"])
    hidden = tmp_path / "nascosto"
    shutil.move(p, hidden)

    assert not listed(alice, up["recording_id"])            # niente "Fermo: …", semplicemente non c'è
    st, code = admin_conn.execute("SELECT status, error_code FROM ingestion_jobs WHERE id = %s",
                                  (up["job_id"],)).fetchone()
    assert (st, code) == ("annullato", "originale_mancante")
    assert alice.get(f"/api/v1/recordings/{up['recording_id']}/audio").status_code == 410

    shutil.move(hidden, p)                                   # ripristinato (es. da un backup)
    assert listed(alice, up["recording_id"])
    assert admin_conn.execute("SELECT status FROM ingestion_jobs WHERE id = %s",
                              (up["job_id"],)).fetchone()[0] == "errore"   # di nuovo in coda


def test_worker_ignores_missing_original(alice, worker, admin_conn, tmp_path):
    up = upload(alice, audio_with_speech("Il worker non trova il file. Lo ignora. Fine."))
    shutil.move(path_of(admin_conn, up["recording_id"]), tmp_path / "via")
    drain(worker)
    st, code = admin_conn.execute("SELECT status, error_code FROM ingestion_jobs WHERE id = %s",
                                  (up["job_id"],)).fetchone()
    assert (st, code) == ("annullato", "originale_mancante")  # non "errore_definitivo"
    assert not listed(alice, up["recording_id"])


def test_note_stays_when_only_audio_is_missing(alice, worker, admin_conn, tmp_path):
    up = upload(alice, audio_with_speech("La nota resta anche senza audio. Il testo c'è. Fine."))
    drain(worker)
    p = path_of(admin_conn, up["recording_id"])
    shutil.move(p, tmp_path / "via")
    recs = alice.get("/api/v1/recordings?limit=200").json()["elementi"]
    [rec] = [x for x in recs if x["id"] == up["recording_id"]]
    assert rec["nota"] and rec["audio_disponibile"] is False
    entry = alice.get(f"/api/v1/entries/{rec['nota']['id']}").json()
    assert entry["audio_disponibile"] is False and entry["trascrizione_grezza"]
    shutil.move(tmp_path / "via", p)


# -- ascolto ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,mime", [("webm", "audio/webm;codecs=opus"), ("mp4", "audio/mp4")])
def test_playback_copy_has_duration_and_supports_seeking(alice, admin_conn, tmp_path, kind, mime):
    data = real_audio(tmp_path, kind, seconds=4)
    up = upload(alice, data, mime=mime, name=f"v.{kind}")
    r = alice.get(f"/api/v1/recordings/{up['recording_id']}/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    out = tmp_path / "ascolto.mp3"
    out.write_bytes(r.content)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert abs(dur - 4.0) < 0.3                                      # durata dichiarata: il cursore funziona
    assert b"Info" in r.content[:2048] or b"Xing" in r.content[:2048]  # intestazione con durata e indice

    part = alice.get(f"/api/v1/recordings/{up['recording_id']}/audio", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100     # richieste parziali (Safari)

    orig = alice.get(f"/api/v1/recordings/{up['recording_id']}/audio?originale=1")
    assert orig.content == data and "attachment" in orig.headers["content-disposition"]
    assert (playback_path(path_of(admin_conn, up["recording_id"])).stat().st_mode & 0o777) == 0o600


def test_playback_falls_back_to_original_when_not_convertible(alice):
    data = b"non e' audio" + os.urandom(256)
    up = upload(alice, data)
    r = alice.get(f"/api/v1/recordings/{up['recording_id']}/audio")
    assert r.status_code == 200 and r.content == data


# -- calendario --------------------------------------------------------------------------------

def test_calendar_and_day_filter_in_phone_timezone(alice, bruno):
    # 23:30 UTC del 14 marzo = 00:30 del 15 a Roma
    late = upload(alice, audio_with_speech("Vocale di notte. Uno. Due."), recorded_at="2025-03-14T23:30:00+00:00")
    upload(alice, audio_with_speech("Vocale del pomeriggio. Uno. Due."), recorded_at="2025-03-20T15:00:00+01:00")
    upload(alice, audio_with_speech("Altro vocale del pomeriggio. Uno. Due."), recorded_at="2025-03-20T17:00:00+01:00")

    rome = alice.get("/api/v1/calendario", params={"mese": "2025-03", "tz": "Europe/Rome"}).json()
    assert rome["giorni"]["2025-03-15"]["vocali"] == 1 and "2025-03-14" not in rome["giorni"]
    assert rome["giorni"]["2025-03-20"]["vocali"] == 2
    assert rome["primo_giorno"] <= "2025-03-15"
    utc = alice.get("/api/v1/calendario", params={"mese": "2025-03", "tz": "UTC"}).json()
    assert utc["giorni"]["2025-03-14"]["vocali"] == 1

    day = alice.get("/api/v1/recordings", params={"giorno": "2025-03-15", "tz": "Europe/Rome"}).json()
    assert [x["id"] for x in day["elementi"]] == [late["recording_id"]]
    assert day["elementi"][0]["giorno"] == "2025-03-15"
    day20 = alice.get("/api/v1/recordings", params={"giorno": "2025-03-20", "tz": "Europe/Rome"}).json()
    assert day20["totale"] == 2
    assert day20["elementi"][0]["registrato_il"] > day20["elementi"][1]["registrato_il"]  # dal più recente

    assert alice.get("/api/v1/calendario", params={"mese": "2025-3"}).status_code == 422
    assert alice.get("/api/v1/calendario", params={"mese": "2025-13"}).status_code == 422
    assert alice.get("/api/v1/calendario", params={"mese": "2025-03", "tz": "Marte/Base"}).json()["fuso"] == "Europe/Rome"
    assert bruno.get("/api/v1/calendario", params={"mese": "2025-03"}).json()["giorni"] == {}
