"""La conoscenza che cresce: punti, schede dei temi, cose fatte, impegni ripetuti, glossario."""

from __future__ import annotations

import json

import pytest

from app import search
from app.cli import main as cli
from app.pipeline.transcribe import Segment, assemble
from app.routers import chiedi as chiedi_router
from app.pipeline.ollama import OllamaClient

from fakes import fake_embedding
from test_chiedi import chiedi
from test_conoscenza import fake, pulito, transcriber, voci, worker  # noqa: F401
from test_pipeline import drain, job, upload

_giorno = [0]


def nota(client, worker, testo: str) -> dict:  # noqa: F811
    """Un vocale al giorno, in ordine: la conoscenza cresce nel tempo."""
    _giorno[0] += 1
    up = upload(client, testo, recorded_at=f"2026-09-{_giorno[0] % 28 + 1:02d}T{8 + _giorno[0] // 28:02d}:00:00+02:00")
    drain(worker)
    return client.get(f"/api/v1/entries/{job(client, up['job_id'])['entry_id']}").json()


@pytest.fixture(autouse=True)
def _ollama(fake):  # noqa: F811
    search.set_query_embedder(fake_embedding)
    chiedi_router.set_client(OllamaClient("http://ollama.test", transport=fake.transport()))
    yield
    chiedi_router.set_client(None)
    search.set_query_embedder(None)


def scheda(titolo="Nota sul progetto alfa", temi=(("progetto alfa", "Lavoro", "progetto"),), punti=(), azioni=(),
           sotto=None):
    """La riorganizzazione di un vocale: un blocco per tema, con i suoi punti e le cose da fare."""
    blocchi = {n: {"argomento": n, "_sotto": (sotto or {}).get(n), "punti": []} for n, _a, _t in temi}
    for tipo, testo, tm, *dettagli in punti:
        blocchi[tm]["punti"].append({"testo": testo, "tipo": tipo, "quando": None,
                                     "dettagli": [{"testo": d[1], "tipo": d[0], "quando": None}
                                                  for d in (dettagli[0] if dettagli else [])]})
    for cosa, quando, tm in azioni:
        blocchi[tm]["punti"].append({"testo": cosa, "tipo": "da_fare", "quando": quando, "dettagli": []})
    for b in blocchi.values():
        if not b["punti"]:
            b["punti"].append({"testo": f"Si parla di {b['argomento']}", "tipo": "punto", "quando": None,
                               "dettagli": []})
    return json.dumps({"titolo": titolo, "categoria": "Lavoro", "blocchi": list(blocchi.values())})


def tema(client, nome: str) -> dict:
    g = client.get("/api/v1/grafo").json()
    c = next(c for c in g["concetti"] if c["nome"] == nome)
    return client.get(f"/api/v1/temi/{c['id']}").json()


def richieste(fake, chiave: str) -> list[dict]:
    return [r for r in fake.chat_requests if chiave in r["format"]["properties"]]


def punti_di(e: dict) -> list[dict]:
    return [p for b in e["appunti"] for p in b["punti"]]


def test_outline_and_living_theme_card(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(punti=[
        ("decisione", "Usare Postgres per il progetto alfa", "progetto alfa",
         [("fatto", "Postgres regge bene i dati"), ("punto", "Lo conosco già")]),
        ("domanda", "Serve un server dedicato?", "progetto alfa"),
        ("idea", "Aggiungere una mappa delle idee", "progetto alfa")])]
    e1 = nota(alice, worker, "Per il progetto alfa ho deciso di usare Postgres: regge bene i dati e lo conosco già. "
                             "Mi chiedo se serve un server dedicato. Ho un'idea: aggiungere una mappa delle idee.")
    [blocco] = e1["appunti"]
    assert blocco["tema"]["nome"] == "progetto alfa"
    assert [p["tipo"] for p in blocco["punti"]] == ["decisione", "domanda", "idea"]
    assert [d["testo"] for d in blocco["punti"][0]["dettagli"]] == ["Postgres regge bene i dati", "Lo conosco già"]
    assert all(p["secondi"] is not None for p in blocco["punti"])

    t = tema(alice, "progetto alfa")
    assert t["sintesi"].startswith("Stai seguendo progetto alfa")
    assert [x["nota"]["id"] for x in t["appunti"]] == [e1["id"]]

    # Secondo vocale: il tema viene riconosciuto con il suo stato, la decisione cambia.
    fake.chat_queue = [scheda(titolo="Cambio di database per alfa",
                              punti=[("decisione", "Usare SQLite invece di Postgres", "progetto alfa")])]
    e2 = nota(alice, worker, "Sul progetto alfa cambio idea: ho deciso di usare SQLite invece di Postgres.")
    colloca = richieste(fake, "collocazione")[-1]["messages"][1]["content"]
    assert "1. progetto alfa (1 nota) — Stai seguendo progetto alfa" in colloca and "Area" not in colloca
    assert t["id"] in [c["id"] for c in e2["concetti"]]                       # stesso tema, non uno nuovo
    agg = richieste(fake, "sintesi")[-1]["messages"][1]["content"]
    assert "1. [deciso," in agg and "Usare Postgres" in agg and "Stato attuale: Stai seguendo" in agg

    t = tema(alice, "progetto alfa")
    assert [x["nota"]["id"] for x in t["appunti"]] == [e2["id"], e1["id"]]   # dal più recente
    vecchia = t["appunti"][1]["punti"][0]
    assert vecchia["testo"] == "Usare Postgres per il progetto alfa" and vecchia["stato"] == "superato"
    assert t["appunti"][0]["punti"][0]["stato"] == "attivo"


def test_two_topics_and_a_subproject(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(titolo="Lavori della settimana",
                              temi=[("sito Rossi", "Lavoro", "progetto"), ("gestionale Bianchi", "Lavoro", "progetto")],
                              punti=[("fatto", "Il sito Rossi è online", "sito Rossi"),
                                     ("domanda", "Il gestionale Bianchi va migrato?", "gestionale Bianchi")])]
    e1 = nota(alice, worker, "Il sito Rossi è online da ieri. Per il gestionale Bianchi non so se va migrato.")
    assert [b["tema"]["nome"] for b in e1["appunti"]] == ["sito Rossi", "gestionale Bianchi"]

    # Uno sviluppo interno del sito: un sotto-tema, dentro il sito.
    fake.chat_queue = [scheda(titolo="Pagamenti del sito Rossi", temi=[("pagamenti online", "Casa", "progetto")],
                              sotto={"pagamenti online": "sito Rossi"},
                              azioni=[("Attivare i pagamenti online", None, "pagamenti online")])]
    nota(alice, worker, "Per il sito Rossi devo attivare i pagamenti online entro il mese.")
    g = alice.get("/api/v1/grafo").json()
    per_nome = {c["nome"]: c for c in g["concetti"]}
    sito, pag = per_nome["sito Rossi"], per_nome["pagamenti online"]
    assert pag["padre"] == sito["id"] and sito["padre"] is None
    t = alice.get(f"/api/v1/temi/{sito['id']}").json()
    assert [x["nome"] for x in t["sotto"]] == ["pagamenti online"] and t["sotto"][0]["aperte"] == 1
    assert [a["testo"] for a in t["azioni"]] == ["Attivare i pagamenti online"]  # anche quelle dei sotto-temi
    assert t["azioni"][0]["tema"]["nome"] == "pagamenti online"
    tp = alice.get(f"/api/v1/temi/{pag['id']}").json()
    assert [x["nome"] for x in tp["percorso"]] == ["sito Rossi"]

    # Un vocale successivo sul sotto-tema lo ritrova per nome, al suo posto.
    fake.chat_queue = [scheda(titolo="Pagamenti attivati sul sito", temi=[("pagamenti online", "Lavoro", "progetto")],
                              punti=[("fatto", "Pagamenti online attivati", "pagamenti online")])]
    e3 = nota(alice, worker, "I pagamenti online sono attivati. Funzionano.")
    assert [c["id"] for c in e3["concetti"]] == [pag["id"]]


def test_ungrounded_points_are_dropped(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(punti=[("fatto", "Il fornitore ha consegnato in Brasile", "progetto alfa"),
                                     ("fatto", "Prototipo del progetto alfa finito", "progetto alfa")])]
    e = nota(alice, worker, "Il prototipo del progetto alfa è finito. Ora si prova.")
    assert [p["testo"] for p in punti_di(e)] == ["Prototipo del progetto alfa finito"]
    assert any("non trova riscontro" in w for w in e["analisi"]["avvisi"])


def test_correction_always_updates_the_notes(alice, worker, fake, admin_conn, pulito):  # noqa: F811
    fake.chat_queue = [scheda(punti=[("fatto", "Il progetto alfa costa mille euro", "progetto alfa")])]
    e = nota(alice, worker, "Il progetto alfa costa mille euro. Va bene.")
    # Una nota passata da una rianalisi: anche così la correzione rifà gli appunti.
    admin_conn.execute("UPDATE ingestion_jobs SET kind = 'rianalisi' WHERE entry_id = %s", (e["id"],))
    testo = e["trascrizione_grezza"].replace("mille", "duemila")
    fake.chat_queue = [scheda(punti=[("fatto", "Il progetto alfa costa duemila euro", "progetto alfa")])]
    r = alice.put(f"/api/v1/entries/{e['id']}/trascrizione", json={"testo": testo, "base": e["trascrizione_sha"]})
    assert r.status_code == 200 and r.json()["rianalisi"] is True
    drain(worker)
    e2 = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e2["stato"] == "archiviazione_completata"
    assert [p["testo"] for p in punti_di(e2)] == ["Il progetto alfa costa duemila euro"]


def test_repeated_commitment_is_not_duplicated_and_done_closes_it(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(azioni=[("Chiamare il commercialista", None, "progetto alfa")])]
    e1 = nota(alice, worker, "Per il progetto alfa devo chiamare il commercialista. Poi si vede.")
    [a] = e1["action_items"]

    fake.chat_queue = [scheda(titolo="Ancora sul commercialista oggi",
                              azioni=[("Chiamare il commercialista", "venerdì", "progetto alfa")])]
    e2 = nota(alice, worker, "Ricordo che devo chiamare il commercialista entro venerdì. Importante.")
    assert e2["action_items"] == []                                       # non duplicata
    assert e2["altre_azioni"][0]["effetto"] == "ribadita"
    e1 = alice.get(f"/api/v1/entries/{e1['id']}").json()
    assert e1["action_items"][0]["ribadita"] == 1 and e1["action_items"][0]["scadenza"] == "venerdì"
    g = alice.get("/api/v1/grafo").json()
    assert [x["testo"] for x in g["azioni"] if x["stato"] == "aperta"] == ["Chiamare il commercialista"]

    # Un vocale dopo dice che è fatta: si chiude, con il vocale che lo dice.
    fake.chat_queue = [scheda(titolo="Giornata piena di commissioni")]
    e3 = nota(alice, worker, "Stamattina ho chiamato il commercialista e abbiamo sistemato. Poi spesa.")
    chiusure = richieste(fake, "fatte")[-1]["messages"][1]["content"]
    assert "1. Chiamare il commercialista" in chiusure and "ho chiamato il commercialista" in chiusure
    a1 = alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]
    assert a1["stato"] == "completata" and a1["chiusa_da"]["id"] == e3["id"]
    assert alice.get(f"/api/v1/entries/{e3['id']}").json()["altre_azioni"][0]["effetto"] == "chiusa"

    # Una nuova analisi del vocale non chiude due volte; eliminarlo riapre la cosa da fare.
    fake.chat_queue = [scheda(titolo="Giornata piena di commissioni")]
    cli(["rianalizza", "--utente", "alice", "--nota", e3["id"][:12]])
    drain(worker)
    ev = alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]
    assert ev["stato"] == "completata"
    assert alice.delete(f"/api/v1/entries/{e3['id']}").status_code == 200
    assert alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]["stato"] == "aperta"


def test_closure_needs_the_words_in_the_transcript(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(azioni=[("Pagare la bolletta della luce", None, "progetto alfa")])]
    e1 = nota(alice, worker, "Devo pagare la bolletta della luce. Scade presto.")
    fake.chat_queue = [scheda(titolo="Pensieri sulla bolletta luce")]
    fake.chiusure_queue = [json.dumps({"fatte": [{"n": 1, "esito": "fatta", "frase": "ho pagato la bolletta"}]})]
    e2 = nota(alice, worker, "La bolletta della luce è arrivata altissima. Non so cosa fare.")
    assert richieste(fake, "fatte")                                     # il modello l'ha detta fatta…
    assert e2["stato"] == "archiviazione_completata"
    assert alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]["stato"] == "aperta"   # …ma non è vero


def test_old_note_reanalysis_does_not_rewind_theme(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(punti=[("fatto", "Progetto alfa avviato", "progetto alfa")])]
    e1 = nota(alice, worker, "Il progetto alfa è avviato. Bene.")
    fake.chat_queue = [scheda(titolo="Seconda nota progetto alfa",
                              punti=[("fatto", "Progetto alfa quasi finito", "progetto alfa")])]
    nota(alice, worker, "Il progetto alfa è quasi finito. Ottimo.")
    prima = tema(alice, "progetto alfa")["sintesi"]
    n = len(richieste(fake, "sintesi"))
    fake.chat_queue = [scheda(punti=[("fatto", "Progetto alfa avviato", "progetto alfa")])]
    cli(["rianalizza", "--utente", "alice", "--nota", e1["id"][:12]])
    drain(worker)
    assert len(richieste(fake, "sintesi")) == n                         # il tema non torna indietro
    assert tema(alice, "progetto alfa")["sintesi"] == prima


def test_full_rebuild_resets_and_regrows(alice, worker, fake, pulito, admin_conn):  # noqa: F811
    fake.chat_queue = [scheda(azioni=[("Chiamare il commercialista", None, "progetto alfa")])]
    e1 = nota(alice, worker, "Per il progetto alfa devo chiamare il commercialista. Poi si vede.")
    fake.chat_queue = [scheda(titolo="Commissioni fatte oggi pomeriggio")]
    nota(alice, worker, "Oggi ho chiamato il commercialista. Tutto a posto.")
    assert alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]["stato"] == "completata"
    fake.chat_queue = [scheda(azioni=[("Chiamare il commercialista", None, "progetto alfa")]),
                       scheda(titolo="Commissioni fatte oggi pomeriggio")]
    cli(["rianalizza", "--utente", "alice", "--tutte", "--da-capo"])
    assert admin_conn.execute("SELECT count(*) FROM punti").fetchone()[0] == 0
    assert alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]["stato"] == "aperta"
    drain(worker)
    a = alice.get(f"/api/v1/entries/{e1['id']}").json()["action_items"][0]
    assert a["stato"] == "completata"                                      # richiusa, in ordine di tempo
    assert tema(alice, "progetto alfa")["sintesi"]


def test_theme_card_isolation(alice, bruno, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda()]
    nota(alice, worker, "Il progetto alfa va avanti. Bene.")
    t = tema(alice, "progetto alfa")
    assert bruno.get(f"/api/v1/temi/{t['id']}").status_code == 404


def test_ask_uses_theme_card(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [scheda(temi=[("ristrutturazione bagno", "Casa", "progetto")],
                              punti=[("decisione", "Piastrelle grigie per il bagno", "ristrutturazione bagno")])]
    nota(alice, worker, "Per la ristrutturazione del bagno ho deciso piastrelle grigie per il bagno.")
    testa, _, _ = chiedi(alice, "a che punto è la ristrutturazione del bagno con le piastrelle?")
    temi = [f for f in testa["fonti"] if f["tipo"] == "tema"]
    assert temi and temi[0]["titolo"] == "ristrutturazione bagno" and temi[0]["tema_id"]
    domanda = fake.chat_requests[-1]["messages"][1]["content"]
    assert "scheda del tema" in domanda and "Deciso: Piastrelle grigie per il bagno" in domanda


def test_glossary_reaches_transcriber(alice, worker, fake, transcriber, pulito):  # noqa: F811
    fake.chat_queue = [scheda(temi=[("officina meccanica", "Lavoro", "tema")])]
    e = nota(alice, worker, "Oggi in officina meccanica. Tanto lavoro.")
    alice.put(f"/api/v1/entries/{e['id']}/voci", json={"nomi": {"1": "Gianluca"}})
    nota(alice, worker, "Altra giornata. Niente di nuovo.")
    g = transcriber.glossari[-1]
    assert "Gianluca" in g and "officina meccanica" in g


def test_paragraphs_follow_sentences_and_pauses():
    testo, segs = assemble([Segment(0, 2, " Prima frase."), Segment(3.5, 5, " Dopo una pausa."),
                            Segment(5.2, 6, " continua"), Segment(6.1, 7, " senza punto"),
                            Segment(8.0, 9, " e qui no."), Segment(12, 13, " Pausa lunga")])
    assert testo == "Prima frase.\n\nDopo una pausa. continua senza punto e qui no.\n\nPausa lunga"
    assert all(testo[s["char_start"]:s["char_end"]] for s in segs)


def test_retranscribe_everything(alice, worker, fake, transcriber, pulito):  # noqa: F811
    fake.chat_queue = [scheda(temi=[("officina meccanica", "Lavoro", "tema")],
                              azioni=[("Ordinare i bulloni", None, "officina meccanica")])]
    e1 = nota(alice, worker, "Oggi in officina meccanica. Devo ordinare i bulloni. Fine.")
    [a] = e1["action_items"]
    alice.patch(f"/api/v1/action-items/{a['id']}", json={"stato": "completata"})       # toccata a mano: resta
    fake.chat_queue = [scheda(titolo="Seconda nota da correggere")]
    e2 = nota(alice, worker, "Una nota con un errore. Lo correggo. Fine.")
    testo = e2["trascrizione_grezza"].replace("errore", "refuso")
    fake.chat_queue = [scheda(titolo="Seconda nota corretta a mano")]
    assert alice.put(f"/api/v1/entries/{e2['id']}/trascrizione",
                     json={"testo": testo, "base": e2["trascrizione_sha"]}).status_code == 200
    drain(worker)

    chiamate = transcriber.calls
    fake.chat_queue = [scheda(temi=[("officina meccanica", "Lavoro", "tema")],
                              azioni=[("Ordinare i bulloni", None, "officina meccanica")]),
                       scheda(titolo="Seconda nota corretta a mano")]
    cli(["rianalizza", "--utente", "alice", "--tutte", "--da-capo", "--ritrascrivi"])
    drain(worker)
    assert transcriber.calls == chiamate + 1                       # la nota corretta a mano non si ritrascrive
    assert "officina meccanica" in transcriber.glossari[-1]        # glossario preso prima di ripartire da capo
    e1 = alice.get(f"/api/v1/entries/{e1['id']}").json()
    assert e1["stato"] == "archiviazione_completata"
    [a] = e1["action_items"]
    assert a["stato"] == "completata" and e1["trascrizione_grezza"][a["char_start"]:a["char_end"]] == a["fonte"]
    assert alice.get(f"/api/v1/entries/{e2['id']}").json()["trascrizione_grezza"] == testo
