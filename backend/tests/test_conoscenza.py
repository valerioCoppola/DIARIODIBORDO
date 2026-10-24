"""Conoscenza e voci: argomenti riusati, chi parla, azioni brevi, rianalisi (l'albero: test_albero.py)."""

from __future__ import annotations

import json

import pytest

from app import search
from app.cli import main as cli
from app.config import get_settings
from app.voci import assegna_voci
from app.voci import testo_con_voci as dialogo
from app.worker import Worker

from fakes import FakeOllama, FakeTranscriber, FakeVoci, fake_embedding, make_pipeline
from test_pipeline import drain, job, upload


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
def voci():
    return FakeVoci()


@pytest.fixture
def worker(fake, transcriber, voci):
    w = Worker(make_pipeline(fake, transcriber, voci=voci), worker_id="test-conoscenza")
    drain(w)
    return w


def risposta(titolo="Nota di prova per oggi", argomenti=(), azioni=(), punti=(), **kw) -> str:
    """La riorganizzazione di un vocale: un blocco per argomento, le cose da fare nel primo blocco.

    Ogni argomento è (nome, certezza della collocazione o None, [tema di cui è una parte]): il
    finto modello lo colloca così (vedi fakes.FakeOllama._colloca)."""
    blocchi = [{"argomento": n, "_certezza": c, "_sotto": sotto[0] if sotto else None,
                "punti": [{"testo": f"Si parla di {n}", "tipo": "punto", "quando": None, "dettagli": []}]}
               for n, c, *sotto in (argomenti or [("progetto Alfa", None)])]
    for c, _chi, q in azioni:
        blocchi[0]["punti"].append({"testo": c, "tipo": "da_fare", "quando": q, "dettagli": []})
    for tipo, testo, *dettagli in punti:
        blocchi[0]["punti"].append({"testo": testo, "tipo": tipo, "quando": None,
                                    "dettagli": [{"testo": d, "tipo": "punto", "quando": None}
                                                 for d in (dettagli[0] if dettagli else [])]})
    return json.dumps({"titolo": titolo, "categoria": "Lavoro", "blocchi": blocchi, **kw})


def nota(client, worker, testo: str) -> dict:
    up = upload(client, testo)
    drain(worker)
    return client.get(f"/api/v1/entries/{job(client, up['job_id'])['entry_id']}").json()


@pytest.fixture
def pulito(admin_conn):
    """Ogni test parte da un archivio senza note, aree né argomenti."""
    for t in ("action_item_events", "action_items", "punti", "entry_topics", "text_chunks", "entry_versions",
              "transcriptions", "model_usage", "job_events", "ingestion_jobs", "entries", "upload_sessions",
              "recordings", "blocchi", "chiarimenti", "indicazioni", "stato_albero", "topics"):
        admin_conn.execute(f"DELETE FROM {t}")


# -- azioni ------------------------------------------------------------------------------

def test_long_actions_are_shortened(alice, worker, fake, pulito):
    lunga = "chiamare il fornitore dei bulloni per chiedere se può consegnare tutto entro la fine del mese"
    fake.chat_queue = [risposta(argomenti=[("fornitore bulloni", None)], azioni=[(lunga, None, None)]),
                       risposta(argomenti=[("fornitore bulloni", None)],
                                azioni=[("chiamare il fornitore dei bulloni", None, None)])]
    e = nota(alice, worker, "Devo chiamare il fornitore dei bulloni. Vediamo se consegna. Il mese finisce.")
    note = [r for r in fake.chat_requests if "blocchi" in r["format"]["properties"]]
    assert "al massimo 6 parole" in note[-1]["messages"][-1]["content"]
    [a] = e["action_items"]
    assert a["descrizione"] == "Chiamare il fornitore dei bulloni"


def test_long_transcript_is_reorganized_in_parts(alice, fake, transcriber, pulito):
    w = Worker(make_pipeline(fake, transcriber, max_context=4096), worker_id="test-consolida")
    drain(w)
    frasi = [f"Al punto {i} si discute del capitolo {i} del manuale tecnico." for i in range(160)]
    for k in range(10):
        frasi[k * 15 + 3] = f"Devo correggere la tabella {k} del manuale."
    up = upload(alice, " ".join(frasi))
    drain(w)
    e = alice.get(f"/api/v1/entries/{job(alice, up['job_id'])['entry_id']}").json()
    assert e["stato"] == "archiviazione_completata"
    richieste = [r for r in fake.chat_requests if "blocchi" in r["format"]["properties"]]
    assert len(richieste) > 2 and "Parte 1 di" in richieste[0]["messages"][1]["content"]
    assert "Appunti delle parti" in richieste[-1]["messages"][1]["content"]     # le parti unite alla fine
    fare = [p for b in e["appunti"] for p in b["punti"] if p["tipo"] == "da_fare"]
    assert fare and all(p["azione"] for p in fare)
    assert all(a["fonte"] in frasi for a in e["action_items"])                 # citazioni dal testo intero
    assert any("riorganizzato in" in x for x in e["analisi"]["avvisi"])


# -- voci ----------------------------------------------------------------------------------

def test_assign_voices_by_overlap():
    segs = [{"inizio": 0, "fine": 2, "char_start": 0, "char_end": 5},
            {"inizio": 2.2, "fine": 5, "char_start": 6, "char_end": 12},
            {"inizio": 5.1, "fine": 6, "char_start": 13, "char_end": 18},
            {"inizio": 30, "fine": 31, "char_start": 19, "char_end": 24}]
    turni = [(0, 2.5, "SPEAKER_03"), (2.5, 4.0, "SPEAKER_01"), (4.0, 6.5, "SPEAKER_01")]
    out, n = assegna_voci(segs, turni)
    assert [s.get("voce") for s in out] == [1, 2, 2, None] and n == 2   # numerate dalla prima che parla
    testo = "Ciao. Si va. Bene. Fine?"
    assert dialogo(testo, out, {"2": "Giulia"}) == "Voce 1: Ciao.\n\nGiulia: Si va. Bene.\n\nVoce ?: Fine?"


def test_voices_reach_analysis_and_owners(alice, worker, fake, voci, pulito):
    fake.chat_queue = [risposta(argomenti=[("sito", None)], azioni=[("preparare il preventivo", "Voce 2", "lunedì"),
                                        ("sentire il cliente", "Voce 7", None)])]
    e = nota(alice, worker, "Facciamo il punto sul sito. Lo preparo io il preventivo entro lunedì. "
                            "Va bene, io sento il cliente.")
    assert len(voci.calls) == 1
    prompt = next(r for r in fake.chat_requests if "blocchi" in r["format"]["properties"])["messages"][1]["content"]
    assert "Voce 1: Facciamo il punto sul sito." in prompt and "Voce 2: Lo preparo io" in prompt
    assert e["voci"]["persone"] == 2 and e["voci"]["stato"] == "ok"
    assert [s["voce"] for s in e["segmenti"]] == [1, 2, 1]
    a1, a2 = e["action_items"]
    assert a1["scadenza"] == "lunedì"

    r = alice.put(f"/api/v1/entries/{e['id']}/voci", json={"nomi": {"1": "Marco", "2": " Giulia "}})
    assert r.status_code == 200 and r.json()["nomi"] == {"1": "Marco", "2": "Giulia"}
    e = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e["voci"]["nomi"] == {"1": "Marco", "2": "Giulia"}
    alice.put(f"/api/v1/entries/{e['id']}/voci", json={"nomi": {"2": ""}})
    e = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e["voci"]["nomi"] == {"1": "Marco"}


def test_voices_never_block_the_note(alice, fake, transcriber, pulito):
    for v, stato in ((FakeVoci(pronto=False), "non_preparato"), (FakeVoci(), "errore")):
        if stato == "errore":
            v.fail = RuntimeError("CUDA out of memory")
        w = Worker(make_pipeline(fake, transcriber, voci=v), worker_id="test-voci-ko")
        e = nota(alice, w, "Una nota qualsiasi. Con due frasi. Anzi tre.")
        assert e["stato"] == "archiviazione_completata" and e["voci"]["stato"] == stato
        assert all("voce" not in s for s in e["segmenti"])


# -- rianalisi ------------------------------------------------------------------------------

def test_reanalysis_keeps_transcript_and_checked_actions(alice, worker, fake, transcriber, voci, pulito):
    testo = "Devo pagare la bolletta della luce. Poi devo chiamare la banca. Il resto domani."
    fake.chat_queue = [risposta(argomenti=[("bollette", None)],
                                azioni=[("pagare la bolletta della luce", None, None),
                                        ("chiamare la banca subito per il mutuo della casa", None, None)])]
    e = nota(alice, worker, testo)
    assert transcriber.calls == 1 and len(voci.calls) == 1
    fatta = e["action_items"][0]
    alice.patch(f"/api/v1/action-items/{fatta['id']}", json={"stato": "completata"})

    fake.chat_queue = [risposta(argomenti=[("bollette", None)],
                                azioni=[("Pagare la luce", None, None), ("Chiamare la banca", None, None)])]
    cli(["rianalizza", "--nota", e["id"][:8], "--utente", "alice"])
    drain(worker)
    e2 = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert transcriber.calls == 1 and len(voci.calls) == 1            # niente nuova trascrizione né voci
    assert e2["stato"] == "archiviazione_completata" and e2["analisi"]["versione"] > e["analisi"]["versione"]
    assert e2["trascrizione_grezza"] == testo
    stati = {a["descrizione"]: a["stato"] for a in e2["action_items"]}
    assert stati == {"Pagare la luce": "completata", "Chiamare la banca": "aperta"}   # spuntata: resta, col testo nuovo

    cli(["rianalizza", "--nota", e["id"][:8], "--voci", "--persone", "2", "--utente", "alice"])
    drain(worker)
    assert transcriber.calls == 1 and len(voci.calls) == 2 and voci.calls[-1][1] == 2


def test_reanalysis_from_scratch_rebuilds_the_tree(alice, worker, fake, pulito, capsys):
    fake.chat_queue = [risposta(argomenti=[("vecchio tema", None, "vecchia cosa")])]
    nota(alice, worker, "Prima nota di prova sul vecchio tema e sul tema nuovo. Due frasi. Tre frasi.")
    fake.chat_queue = [risposta(argomenti=[("tema nuovo", None)])]
    cli(["rianalizza", "--tutte", "--da-capo", "--utente", "alice"])
    assert "In coda: 1 nota" in capsys.readouterr().out
    drain(worker)
    g = alice.get("/api/v1/grafo").json()
    assert {c["nome"] for c in g["concetti"]} == {"tema nuovo"}
