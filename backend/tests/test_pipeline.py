"""Pipeline completa sul database reale: worker, fasi, errori, ripresa, coda."""

from __future__ import annotations

import json
import os

import pytest

from app import search
from app.note_schema import Nota
from app.worker import Worker

from fakes import FakeOllama, FakeTranscriber, audio_with_speech, fake_embedding, make_pipeline

TESTO = ("Oggi riunione con Marco sul progetto Alfa. Devo inviare il preventivo a Marco entro venerdì. "
         "Il cliente vuole vedere una demo prima di firmare. Poi ho pensato a come ridurre i costi del server.")


@pytest.fixture(autouse=True)
def _query_embedder():
    search.set_query_embedder(fake_embedding)
    yield
    search.set_query_embedder(None)


@pytest.fixture
def fake():
    return FakeOllama()


@pytest.fixture
def transcriber():
    return FakeTranscriber()


@pytest.fixture
def worker(fake, transcriber):
    w = Worker(make_pipeline(fake, transcriber), worker_id="test-worker")
    drain(w)  # job lasciati da altri test: si parte da coda vuota
    return w


def drain(w: Worker, limit: int = 100) -> int:
    n = 0
    while w.run_once():
        n += 1
        assert n < limit, "il worker non si ferma"
    return n


def upload(client, text: str, recorded_at: str | None = "2026-10-20T18:30:00+02:00") -> dict:
    data = {"source": "registratore"}
    if recorded_at:
        data |= {"recorded_at": recorded_at, "recorded_at_source": "registratore_integrato"}
    r = client.post("/api/v1/recordings", files={"file": ("vocale.webm", audio_with_speech(text), "audio/webm")},
                    data=data)
    assert r.status_code == 201, r.text
    return r.json()


def job(client, job_id) -> dict:
    return client.get(f"/api/v1/jobs/{job_id}").json()


def make_due(admin_conn, job_id) -> None:
    """Salta l'attesa prima del prossimo tentativo."""
    admin_conn.execute("UPDATE ingestion_jobs SET next_attempt_at = now() WHERE id = %s", (job_id,))


# -- flusso completo ------------------------------------------------------------------

def test_full_pipeline(alice, worker, fake, admin_conn):
    # Un impegno uguale già aperto da un altro test verrebbe "ribadito", non duplicato.
    admin_conn.execute("DELETE FROM action_items")
    up = upload(alice, TESTO)
    assert drain(worker) == 1

    j = job(alice, up["job_id"])
    assert j["stato"] == "archiviazione_completata" and j["concluso"] and j["errore"] is None
    assert [e["a"] for e in j["eventi"]] == [
        "ricevuto", "originale_conservato", "trascrizione_in_corso", "trascrizione_completata", "analisi_in_corso",
        "dati_strutturati_salvati", "indicizzazione_completata", "archiviazione_completata"]

    e = alice.get(f"/api/v1/entries/{j['entry_id']}").json()
    assert e["stato"] == "archiviazione_completata"
    assert e["titolo"] == "Oggi riunione con Marco" and e["categoria"] == "Lavoro"
    assert e["tag"] == ["progetto Alfa"]                    # il tema in cui è stato collocato l'argomento
    assert len(e["executive_summary"]) == 3               # dai primi punti, per il formato canonico
    [blocco] = e["appunti"]
    assert blocco["tema"]["nome"] == "progetto Alfa"
    assert [p["tipo"] for p in blocco["punti"]] == ["fatto", "da_fare", "fatto", "fatto"]
    assert all(p["secondi"] is not None for p in blocco["punti"])
    assert blocco["punti"][1]["azione"]["stato"] == "aperta"
    assert e["data_ora"] == "2026-10-20T18:30:00+02:00"   # dai metadati, con l'offset originale
    assert e["trascrizione_grezza"] == TESTO              # mai riscritta
    assert e["analisi"]["versione"] == 1 and e["analisi"]["prompt"] == "appunti-1+colloca-2"

    # Azione con la frase che la giustifica, alla sua posizione esatta
    [a] = e["action_items"]
    assert a["descrizione"].lower().startswith("inviare il preventivo")
    assert a["fonte"] == "Devo inviare il preventivo a Marco entro venerdì."
    assert TESTO[a["char_start"]:a["char_end"]] == a["fonte"]

    # Segmenti temporizzati che puntano al testo
    assert e["segmenti"] and all(TESTO[s["char_start"]:s["char_end"]].strip() for s in e["segmenti"])
    assert e["segmenti"][1]["inizio"] > 0

    # JSON canonico valido contro lo schema
    exported = alice.get(f"/api/v1/entries/{j['entry_id']}/nota.json")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    nota = Nota.model_validate(exported.json())
    assert nota.trascrizione_grezza == TESTO and nota.action_items[0].descrizione == a["descrizione"]

    # Segmenti indicizzati con embedding a 1024 dimensioni
    rows = admin_conn.execute("SELECT char_start, char_end, content, vector_dims(embedding), embedding_model "
                              "FROM text_chunks WHERE entry_id = %s ORDER BY ord", (j["entry_id"],)).fetchall()
    assert rows and all(TESTO[r[0]:r[1]] == r[2] and r[3] == 1024 and r[4] == "bge-m3" for r in rows)

    ops = {r[0] for r in admin_conn.execute("SELECT operation FROM model_usage WHERE job_id = %s", (up["job_id"],))}
    assert ops == {"trascrizione", "analisi", "embedding"}

    # Richiesta a Ollama: JSON vincolato dallo schema, niente ragionamento, temperatura 0
    req = fake.chat_requests[-1]
    assert req["think"] is False and req["format"]["required"] and req["options"]["temperature"] == 0
    assert req["options"]["num_ctx"] >= 4096 and req["stream"] is False

    # L'archivio mostra la nota accanto alla registrazione
    rec = alice.get(f"/api/v1/recordings/{up['recording_id']}").json()
    assert rec["nota"]["titolo"] == "Oggi riunione con Marco"
    assert alice.get("/api/v1/entries").json()["totale"] >= 1


def test_note_without_recorded_date_has_no_date(alice, worker):
    up = upload(alice, "Una nota caricata senza data. Parla di giardinaggio. E di rose.", recorded_at=None)
    drain(worker)
    e = alice.get(f"/api/v1/entries/{job(alice, up['job_id'])['entry_id']}").json()
    assert e["data_ora"] is None                          # nessuna data inventata


# -- nessun fatto fuori dalla fonte -------------------------------------------------------

def _blocchi(*punti, argomento="magazzino") -> list[dict]:
    return [{"argomento": argomento, "punti": [{"testo": t, "tipo": tipo, "quando": q, "dettagli": d}
                                               for tipo, t, q, d in punti]}]


def test_grounding_removes_unsupported_details(alice, worker, fake):
    testo = "Devo chiamare il fornitore per i bulloni. Il magazzino è quasi vuoto. Ne parlo domani."
    fake.chat_queue = [json.dumps({
        "titolo": "Bulloni e magazzino vuoto", "categoria": "Lavoro",
        "blocchi": _blocchi(
            ("fatto", "Il magazzino è quasi vuoto", None,
             [{"testo": "Mancano anche le viti da legno in Brasile", "tipo": "fatto", "quando": None}]),
            ("da_fare", "chiamare il fornitore dei bulloni", "lunedì", []),
            ("da_fare", "comprare un'automobile nuova", None, [])),
    })]
    up = upload(alice, testo)
    drain(worker)
    e = alice.get(f"/api/v1/entries/{job(alice, up['job_id'])['entry_id']}").json()
    [a] = e["action_items"]                                  # l'automobile non è nel testo: scartata
    assert a["descrizione"] == "Chiamare il fornitore dei bulloni"
    assert a["scadenza"] is None                             # lunedì non è nel testo
    [b] = e["appunti"]
    assert b["punti"][0]["dettagli"] == []                   # il dettaglio inventato è scartato
    avvisi = " | ".join(e["analisi"]["avvisi"])
    assert "lunedì" in avvisi and "automobile" in avvisi and "Brasile" in avvisi


def test_invalid_answers_are_corrected(alice, worker, fake):
    fake.chat_queue = ["non è json", json.dumps({"titolo": "Titolo di prova qui", "categoria": "Idea"})]
    up = upload(alice, "Ho un'idea per un'app di ricette. Si fotografa il frigo. Propone cosa cucinare.")
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "archiviazione_completata"
    note = [r for r in fake.chat_requests if "blocchi" in r["format"]["properties"]]
    assert len(note) == 3
    assert "blocchi" in note[2]["messages"][-1]["content"]   # il modello riceve l'errore da correggere
    e = alice.get(f"/api/v1/entries/{j['entry_id']}").json()
    assert any("tentativo 3" in w for w in e["analisi"]["avvisi"])


def test_title_length_is_requested_then_warned(alice, worker, fake):
    risposta = {"titolo": "Troppo lungo questo titolo per la nota di oggi", "categoria": "Altro",
                "blocchi": _blocchi(("fatto", "Una nota qualsiasi senza azioni", None, []), argomento="varie")}
    fake.chat_queue = [json.dumps(risposta)] * 3
    up = upload(alice, "Una nota qualsiasi. Senza azioni. Fine.")
    drain(worker)
    e = alice.get(f"/api/v1/entries/{job(alice, up['job_id'])['entry_id']}").json()
    assert "da 3 a 6 parole" in fake.chat_requests[1]["messages"][-1]["content"]
    assert any("titolo ha 9 parole" in w for w in e["analisi"]["avvisi"])


# -- errori, ripresa, coda ------------------------------------------------------------------

def test_resume_from_failed_stage(alice, worker, fake, transcriber, admin_conn):
    fake.down = True
    up = upload(alice, "Ollama è spento. La trascrizione però è fatta. Va solo analizzata.")
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore" and j["fase_errore"] == "analisi_in_corso"
    assert j["codice_errore"] == "ollama_non_raggiungibile"
    assert j["tentativi"] == 0                                # servizio spento: tentativo restituito
    assert transcriber.calls == 1 and j["entry_id"]
    n_events = len(j["eventi"])

    # Ancora spento: nessun nuovo evento a ogni minuto
    make_due(admin_conn, up["job_id"])
    drain(worker)
    assert len(job(alice, up["job_id"])["eventi"]) == n_events

    fake.down = False
    make_due(admin_conn, up["job_id"])
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "archiviazione_completata"
    assert transcriber.calls == 1                             # la trascrizione non è stata rifatta


def test_retry_backoff_definitive_and_manual_retry(alice, worker, fake, admin_conn):
    fake.bad_answers = True
    up = upload(alice, "Il modello risponde sempre male. Poi si riprende. Alla fine va.")
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore" and j["tentativi"] == 1 and j["codice_errore"] == "analisi_non_valida"
    wait = admin_conn.execute("SELECT extract(epoch FROM next_attempt_at - now()) FROM ingestion_jobs WHERE id = %s",
                              (up["job_id"],)).fetchone()[0]
    assert 20 < wait <= 30                                     # primo intervallo: 30 s

    for _ in range(10):
        make_due(admin_conn, up["job_id"])
        drain(worker)
        if job(alice, up["job_id"])["stato"] == "errore_definitivo":
            break
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore_definitivo" and j["tentativi"] == 5

    fake.bad_answers = False
    r = alice.post(f"/api/v1/jobs/{up['job_id']}/riprova")
    assert r.status_code == 200
    drain(worker)
    assert job(alice, up["job_id"])["stato"] == "archiviazione_completata"
    assert alice.post(f"/api/v1/jobs/{up['job_id']}/riprova").status_code == 409


def test_no_speech_is_definitive(alice, worker, admin_conn):
    r = alice.post("/api/v1/recordings", files={"file": ("silenzio.webm", b"PARLATO:\x00" + os.urandom(512),
                                                         "audio/webm")})
    up = r.json()
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore_definitivo" and j["codice_errore"] == "nessun_parlato"
    assert j["entry_id"] is None


def test_missing_model_waits_without_consuming_attempts(alice, worker, fake):
    fake.missing_models = {"qwen3:8b"}
    up = upload(alice, "Il modello non è stato scaricato. Va scaricato. Poi funziona.")
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore" and j["codice_errore"] == "modello_non_installato" and j["tentativi"] == 0
    assert "ollama pull qwen3:8b" in j["errore"]


def test_transcription_error_is_retried(alice, worker, transcriber, admin_conn):
    transcriber.fail_next = [RuntimeError("errore improvviso del decoder")]
    up = upload(alice, "Prima la trascrizione fallisce. Poi riesce. Bene così.")
    drain(worker)
    j = job(alice, up["job_id"])
    assert j["stato"] == "errore" and j["fase_errore"] == "trascrizione_in_corso" and j["tentativi"] == 1
    assert "RuntimeError" in j["errore"]
    make_due(admin_conn, up["job_id"])
    drain(worker)
    assert job(alice, up["job_id"])["stato"] == "archiviazione_completata"


def test_model_without_think_parameter(alice, worker, fake):
    fake.reject_think = True
    up = upload(alice, "Un modello che non ragiona. Non accetta think. Si riprova senza.")
    drain(worker)
    assert job(alice, up["job_id"])["stato"] == "archiviazione_completata"
    assert "think" not in fake.chat_requests[-1]


def test_long_transcript_is_analyzed_in_parts(alice, fake, transcriber):
    w = Worker(make_pipeline(fake, transcriber, max_context=4096), worker_id="test-lungo")
    drain(w)
    frasi = [f"Al punto {i} si discute del capitolo {i} del manuale tecnico." for i in range(150)]
    frasi[20] = "Devo stampare il capitolo venti del manuale."
    frasi[130] = "Devo spedire il manuale finito alla tipografia."
    testo = " ".join(frasi)
    up = upload(alice, testo)
    drain(w)
    j = job(alice, up["job_id"])
    assert j["stato"] == "archiviazione_completata"
    e = alice.get(f"/api/v1/entries/{j['entry_id']}").json()
    assert any("parti" in x for x in e["analisi"]["avvisi"])
    assert {a["fonte"] for a in e["action_items"]} == {frasi[20], frasi[130]}
    assert len([r for r in fake.chat_requests if "blocchi" in r["format"]["properties"]]) >= 3


def test_two_workers_never_take_the_same_job(alice, worker, fake, admin_conn):
    a = upload(alice, "Primo vocale per la coda. Una frase. Due frasi.")
    b = upload(alice, "Secondo vocale per la coda. Una frase. Due frasi.")
    w2 = Worker(worker.p, worker_id="secondo")
    first, second = worker.claim(), w2.claim()
    assert first and second and first[0] != second[0]
    assert {str(first[0]), str(second[0])} == {str(a["job_id"]), str(b["job_id"])}
    assert worker.claim() is None                             # entrambi affittati

    # Affitto scaduto (worker morto): il job torna prelevabile
    admin_conn.execute("UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes' WHERE id = %s",
                       (first[0],))
    again = w2.claim()
    assert again and again[0] == first[0]
    for jid, owner, diario in (again, second):
        w2.process(jid, owner, diario)
    for up in (a, b):
        assert job(alice, up["job_id"])["stato"] == "archiviazione_completata"


# -- ricerca, azioni, isolamento ----------------------------------------------------------

def test_search_hybrid_and_text_only(alice, bruno, worker):
    upload(alice, "Ho comprato i semi di basilico per l'orto. Li pianto sul balcone. Serve tanto sole.")
    upload(alice, "La revisione dell'auto è fissata. Bisogna cambiare le gomme. Costo trecento euro.")
    drain(worker)

    r = alice.get("/api/v1/search", params={"q": "basilico"}).json()
    assert r["modalita"] == "ibrida"
    assert r["risultati"] and "basilico" in r["risultati"][0]["frammento"].lower()
    assert "\u0001" in r["risultati"][0]["frammento"]       # parola evidenziata
    assert r["risultati"][0]["secondi"] is not None

    # Forme diverse della stessa parola (dizionario italiano)
    r = alice.get("/api/v1/search", params={"q": "gomma auto"}).json()
    assert r["risultati"] and "gomme" in r["risultati"][0]["frammento"]

    search.set_query_embedder(lambda q: None)              # Ollama spento: solo testuale
    r = alice.get("/api/v1/search", params={"q": "basilico"}).json()
    assert r["modalita"] == "testuale" and r["risultati"]

    # Isolamento: Bruno non trova nulla di Alice
    assert bruno.get("/api/v1/search", params={"q": "basilico"}).json()["risultati"] == []


def test_entry_isolation_and_action_update(alice, bruno, worker, admin_conn):
    up = upload(alice, "Devo rinnovare il passaporto in comune. Serve una foto. E la marca da bollo.")
    drain(worker)
    entry_id = job(alice, up["job_id"])["entry_id"]
    assert bruno.get(f"/api/v1/entries/{entry_id}").status_code == 404
    assert bruno.get(f"/api/v1/entries/{entry_id}/nota.json").status_code == 404
    [a] = alice.get(f"/api/v1/entries/{entry_id}").json()["action_items"]
    assert bruno.patch(f"/api/v1/action-items/{a['id']}", json={"stato": "completata"}).status_code == 404
    r = alice.patch(f"/api/v1/action-items/{a['id']}", json={"stato": "completata"})
    assert r.status_code == 200 and r.json()["stato"] == "completata"
    ev = admin_conn.execute("SELECT event, origin FROM action_item_events WHERE action_item_id = %s",
                            (a["id"],)).fetchall()
    assert [tuple(x) for x in ev] == [("completata", "manuale")]
    assert alice.patch(f"/api/v1/action-items/{a['id']}", json={"stato": "boh"}).status_code == 422


def test_worker_heartbeat_in_status(alice, worker):
    worker.beat()
    s = alice.get("/api/v1/status").json()
    assert s["elaboratore"]["attivo"] is True
    assert s["elaboratore"]["dettagli"]["trascrizione"]["modello"] == "finto"
    assert "in_elaborazione" in s


def test_rls_hides_jobs_from_other_user_in_worker_tables(bruno, alice, worker):
    up = upload(alice, "Una nota privata di Alice. Nessuno deve vederla. Solo lei.")
    drain(worker)
    assert bruno.get(f"/api/v1/jobs/{up['job_id']}").status_code == 404
    assert all(x["id"] != job(alice, up["job_id"])["entry_id"] for x in bruno.get("/api/v1/entries").json()["elementi"])
