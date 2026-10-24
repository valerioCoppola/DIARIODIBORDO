"""Correzione a mano della trascrizione e grafo dell'archivio."""

from __future__ import annotations

import random

import pytest

from app import search
from app.correzione import Mappa, sha
from app.worker import Worker

from fakes import FakeOllama, FakeTranscriber, fake_embedding, make_pipeline
from test_pipeline import TESTO, drain, job, upload


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
    w = Worker(make_pipeline(fake, transcriber), worker_id="test-correzione")
    drain(w)
    return w


def nota_pronta(client, worker, testo: str = TESTO) -> dict:
    up = upload(client, testo)
    drain(worker)
    return client.get(f"/api/v1/entries/{job(client, up['job_id'])['entry_id']}").json()


def frasi(e: dict) -> list[str]:
    t = e["trascrizione_grezza"]
    return [t[s["char_start"]:s["char_end"]].strip() for s in e["segmenti"]]


# -- spostamento delle posizioni ----------------------------------------------------------

def test_mappa_follows_the_words():
    old = "Ho visto Marco ieri. Devo chiamare Luca domani."
    new = "Ho visto Mario ieri sera. Devo chiamare Luca domani."
    m = Mappa(old, new)
    a, b = old.index("Devo"), len(old)
    assert new[m(a):m(b)] == "Devo chiamare Luca domani."
    assert new[m(0):m(old.index(" Devo"))] == "Ho visto Mario ieri sera."


def test_mappa_insert_on_boundary_goes_with_what_follows():
    old = "Prima frase. Seconda frase."
    new = "Prima frase. Anzi, seconda frase."
    m = Mappa(old, new)
    cut = old.index("Seconda")
    assert new[m(0):m(cut - 1)] == "Prima frase."
    assert new[m(cut):m(len(old))] == "Anzi, seconda frase."


def test_mappa_is_monotonic_and_never_crosses():
    rnd = random.Random(7)
    words = "il la mare vento barca porto rotta nord sud vela onda faro molo".split()
    for _ in range(200):
        old = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 40)))
        toks = old.split()
        for _ in range(rnd.randint(0, 6)):
            op = rnd.choice("rid")
            i = rnd.randrange(len(toks) + (op == "i"))
            if op == "r" and toks:
                toks[min(i, len(toks) - 1)] = rnd.choice(words).upper()
            elif op == "i":
                toks.insert(i, rnd.choice(words))
            elif op == "d" and len(toks) > 1:
                del toks[min(i, len(toks) - 1)]
        new = " ".join(toks)
        m = Mappa(old, new)
        vals = [m(p) for p in range(len(old) + 1)]
        assert vals[0] == 0 and vals[-1] == len(new)
        assert all(x <= y for x, y in zip(vals, vals[1:]))


# -- correzione dall'API ------------------------------------------------------------------

def test_correction_moves_segments_and_reanalyzes(alice, worker, fake, transcriber, admin_conn):
    e = nota_pronta(alice, worker)
    prima = frasi(e)
    v_prima = e["analisi"]["versione"]
    trascrizioni = transcriber.calls
    nuovo = e["trascrizione_grezza"].replace("Marco sul progetto Alfa", "Mario sul progetto Alpha") \
                                    .replace("a Marco entro", "a Mario entro")

    r = alice.put(f"/api/v1/entries/{e['id']}/trascrizione", json={"testo": nuovo, "base": e["trascrizione_sha"]})
    assert r.status_code == 200, r.text
    assert r.json()["modificata"] and r.json()["rianalisi"] and r.json()["trascrizione_sha"] == sha(nuovo)

    # Subito: testo nuovo, segmenti che puntano alle frasi corrette, nota in coda per l'analisi.
    e2 = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e2["trascrizione_grezza"] == nuovo and e2["stato"] == "trascrizione_completata"
    dopo = frasi(e2)
    assert len(dopo) == len(prima)
    assert dopo[0] == "Oggi riunione con Mario sul progetto Alpha."
    assert dopo[1] == "Devo inviare il preventivo a Mario entro venerdì."
    assert dopo[2:] == prima[2:]
    assert [s["inizio"] for s in e2["segmenti"]] == [s["inizio"] for s in e["segmenti"]]

    # La versione precedente resta; quella corretta è la corrente.
    versioni = admin_conn.execute("SELECT version, origin, note_json->>'trascrizione_grezza' FROM entry_versions "
                                  "WHERE entry_id = %s ORDER BY version", (e["id"],)).fetchall()
    assert versioni[-1][1:] == ("correzione_manuale", nuovo)
    assert versioni[-2][2] == e["trascrizione_grezza"]

    # Il worker rifà analisi e indice, senza ritrascrivere.
    drain(worker)
    e3 = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e3["stato"] == "archiviazione_completata" and transcriber.calls == trascrizioni
    assert e3["analisi"]["versione"] == v_prima + 2
    assert "Mario" in fake.chat_requests[-1]["messages"][1]["content"]
    [a] = e3["action_items"]
    assert a["fonte"] == "Devo inviare il preventivo a Mario entro venerdì."
    assert nuovo[a["char_start"]:a["char_end"]] == a["fonte"]
    rows = admin_conn.execute("SELECT char_start, char_end, content FROM text_chunks WHERE entry_id = %s",
                              (e["id"],)).fetchall()
    assert rows and all(nuovo[r[0]:r[1]] == r[2] for r in rows)
    trovati = alice.get("/api/v1/search", params={"q": "Alpha"}).json()["risultati"]
    assert any(x["entry_id"] == e["id"] for x in trovati)
    assert alice.get(f"/api/v1/entries/{e['id']}/nota.json").json()["trascrizione_grezza"] == nuovo


def test_ticked_action_keeps_its_quote_in_place(alice, worker):
    e = nota_pronta(alice, worker)
    [a] = e["action_items"]
    assert alice.patch(f"/api/v1/action-items/{a['id']}", json={"stato": "completata"}).status_code == 200
    nuovo = "Premessa aggiunta. " + e["trascrizione_grezza"].replace("entro venerdì", "entro sabato")
    r = alice.put(f"/api/v1/entries/{e['id']}/trascrizione", json={"testo": nuovo, "base": e["trascrizione_sha"]})
    assert r.status_code == 200
    drain(worker)
    e2 = alice.get(f"/api/v1/entries/{e['id']}").json()
    fatta = [x for x in e2["action_items"] if x["id"] == a["id"]]
    assert fatta and fatta[0]["stato"] == "completata"
    assert fatta[0]["fonte"] == "Devo inviare il preventivo a Marco entro sabato."
    assert nuovo[fatta[0]["char_start"]:fatta[0]["char_end"]] == fatta[0]["fonte"]


def test_correction_conflicts(alice, bruno, worker, admin_conn):
    e = nota_pronta(alice, worker)
    url = f"/api/v1/entries/{e['id']}/trascrizione"

    # Testo cambiato nel frattempo: niente sovrascritto.
    r = alice.put(url, json={"testo": "Altro testo.", "base": sha("testo diverso")})
    assert r.status_code == 409 and r.json()["errore"] == "testo_cambiato"

    # Stesso testo: niente da rifare.
    r = alice.put(url, json={"testo": e["trascrizione_grezza"], "base": e["trascrizione_sha"]})
    assert r.status_code == 200 and r.json()["modificata"] is False
    assert alice.get(f"/api/v1/entries/{e['id']}").json()["stato"] == "archiviazione_completata"

    # Vuoto: rifiutato.
    assert alice.put(url, json={"testo": "   \n "}).status_code == 422

    # Il worker ci sta lavorando: si aspetta.
    admin_conn.execute("UPDATE ingestion_jobs SET status = 'analisi_in_corso', locked_at = now() "
                       "WHERE entry_id = %s", (e["id"],))
    r = alice.put(url, json={"testo": e["trascrizione_grezza"] + " Aggiunta.", "base": e["trascrizione_sha"]})
    assert r.status_code == 409 and r.json()["errore"] == "in_elaborazione"
    admin_conn.execute("UPDATE ingestion_jobs SET status = 'archiviazione_completata', locked_at = NULL "
                       "WHERE entry_id = %s", (e["id"],))

    # Nota di un altro utente: non esiste.
    assert bruno.put(url, json={"testo": "Testo di Bruno."}).status_code == 404
    assert alice.get(f"/api/v1/entries/{e['id']}").json()["trascrizione_grezza"] == e["trascrizione_grezza"]


# -- grafo ------------------------------------------------------------------------------

def test_graph_links_concepts_notes_actions(alice, bruno, worker, fake):
    import json as _json

    from test_conoscenza import risposta

    fake.chat_queue = [risposta(titolo="Vela nuova per la barca", argomenti=[("barca", "Casa"), ("porto", "Casa")],
                                azioni=[("comprare la vela nuova", None, None)])]
    e1 = nota_pronta(alice, worker, "Devo comprare la vela nuova per la barca. Il porto è pieno. Domenica si esce.")
    fake.chat_queue = [risposta(titolo="Costi del posto barca", argomenti=[("porto", "Casa"), ("costi", "Finanze")])]
    e2 = nota_pronta(alice, worker, "Il posto nel porto costa troppo, i costi sono alti. Valuto un altro ormeggio.")

    g = alice.get("/api/v1/grafo").json()
    nomi = {c["id"]: c["nome"] for c in g["concetti"]}
    note = {n["id"]: n for n in g["note"]}
    assert {nomi[c] for c in note[e1["id"]]["concetti"]} == {"barca", "porto"}
    assert {nomi[c] for c in note[e2["id"]]["concetti"]} == {"porto", "costi"}
    assert note[e1["id"]]["titolo"] == "Vela nuova per la barca" and note[e1["id"]]["data"]
    [a] = [x for x in g["azioni"] if x["nota"] == e1["id"]]
    assert a["testo"] == "Comprare la vela nuova" and a["stato"] == "aperta"
    questi = [c for c in g["concetti"] if c["nome"] in ("barca", "porto", "costi")]
    assert len(questi) == 3 and all(c["padre"] is None for c in questi)    # tre temi a sé, in cima
    assert len({c["tinta"] for c in questi}) == 3                          # ognuno con la sua tinta
    assert note[e1["id"]]["sintesi"] == "Si parla di barca."

    # Nota eliminata: sparisce con le sue azioni; un concetto rimasto senza note pure.
    assert alice.delete(f"/api/v1/entries/{e1['id']}").status_code == 200
    g = alice.get("/api/v1/grafo").json()
    assert e1["id"] not in {n["id"] for n in g["note"]}
    assert not [x for x in g["azioni"] if x["nota"] == e1["id"]]
    assert "barca" not in {c["nome"] for c in g["concetti"]}

    # Isolamento tra utenti.
    gb = bruno.get("/api/v1/grafo").json()
    assert e2["id"] not in {n["id"] for n in gb["note"]}
