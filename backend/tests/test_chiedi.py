"""Domande sull'archivio: periodi, passaggi pertinenti, cose da fare, risposta in streaming."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import search
from app.chiedi import periodo
from app.pipeline.ollama import OllamaClient
from app.routers import chiedi as chiedi_router

from fakes import fake_embedding
from test_conoscenza import fake, nota, pulito, risposta, transcriber, voci, worker  # noqa: F401
from test_pipeline import upload, drain, job

TZ = timezone(timedelta(hours=2))
ORA = datetime(2026, 10, 21, 18, 0, tzinfo=TZ)        # mercoledì


@pytest.fixture(autouse=True)
def _ollama(fake):
    search.set_query_embedder(fake_embedding)
    chiedi_router.set_client(OllamaClient("http://ollama.test", transport=fake.transport()))
    yield
    chiedi_router.set_client(None)
    search.set_query_embedder(None)


def chiedi(client, domanda: str, **kw) -> tuple[dict, str, list[dict]]:
    r = client.post("/api/v1/chiedi", json={"domanda": domanda, "oggi": ORA.isoformat(), **kw})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    righe = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    return righe[0], "".join(x.get("t", "") for x in righe[1:]), righe[1:]


@pytest.mark.parametrize("domanda,dal,al,nome", [
    ("cosa ho detto ieri sul sito?", "2026-10-20", "2026-10-21", "ieri"),
    ("e l'altro ieri?", "2026-10-19", "2026-10-20", "l'altro ieri"),
    ("la settimana scorsa cosa ho deciso", "2026-10-12", "2026-10-19", "la settimana scorsa"),
    ("di cosa ho parlato a settembre?", "2026-09-01", "2026-10-01", "settembre"),
    ("a dicembre cosa dicevo", "2025-12-01", "2026-01-01", "dicembre"),
    ("lunedì ho chiamato Marco?", "2026-10-19", "2026-10-20", "lunedì"),
    ("negli ultimi 3 giorni", "2026-10-19", "2026-10-22", "gli ultimi 3 giorni"),
])
def test_periods(domanda, dal, al, nome):
    p = periodo(domanda, ORA)
    assert p and p.dal.date().isoformat() == dal and p.al.date().isoformat() == al and p.nome == nome
    assert p.dal.utcoffset() == timedelta(hours=2)                      # nel fuso del telefono


def test_no_period():
    assert periodo("cosa ho deciso sul preventivo?", ORA) is None


def test_answer_cites_relevant_passages(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("preventivi", "Lavoro")])]
    e = nota(alice, worker, "Il preventivo per il sito costa tremila euro. Lo mando a Luca. Poi vediamo.")
    fake.chat_queue = [risposta(argomenti=[("giardino", "Casa")])]
    nota(alice, worker, "Ho potato le rose del giardino. Domenica pioveva. Basta così.")
    fake.chat_queue = ["Il preventivo costa tremila euro [1]."]
    testa, testo, righe = chiedi(alice, "Quanto costa il preventivo del sito?")
    assert testa["fonti"][0]["entry_id"] == e["id"] and testa["fonti"][0]["n"] == 1
    assert all(f["entry_id"] == e["id"] for f in testa["fonti"])         # il giardino non c'entra
    assert testo == "Il preventivo costa tremila euro [1]." and righe[-1] == {"fine": True}
    req = fake.chat_requests[-1]
    assert req["stream"] is True and "format" not in req
    assert "[1] " in req["messages"][1]["content"] and "Domanda: Quanto costa" in req["messages"][1]["content"]


def test_nothing_relevant_skips_the_model(alice, worker, fake, pulito):  # noqa: F811
    nota(alice, worker, "Ho potato le rose del giardino. Domenica pioveva. Basta così.")
    n = len(fake.chat_requests)
    testa, testo, _ = chiedi(alice, "Astronavi marziane?")
    assert testa["fonti"] == [] and testo.startswith("Nel diario non trovo") and len(fake.chat_requests) == n


def test_period_question_reads_summaries(alice, worker, fake, pulito):  # noqa: F811
    up = upload(alice, "Giornata lunga in ufficio. Riunioni tutto il giorno. Stanco.",
                recorded_at="2026-10-20T19:00:00+02:00")
    drain(worker)
    upload(alice, "Un'altra cosa del mese scorso. Niente di che. Fine.", recorded_at="2026-09-03T10:00:00+02:00")
    drain(worker)
    testa, _, _ = chiedi(alice, "Cosa ho fatto ieri?")
    assert testa["periodo"] == "ieri"
    assert [f["entry_id"] for f in testa["fonti"]] == [job(alice, up["job_id"])["entry_id"]]


def test_open_actions_are_included(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(azioni=[("chiamare il commercialista", None, "venerdì")])]
    nota(alice, worker, "Devo chiamare il commercialista entro venerdì. Il resto va bene. Ciao.")
    testa, _, _ = chiedi(alice, "Cosa devo fare?")
    azioni = [f for f in testa["fonti"] if f["tipo"] == "azione"]
    assert azioni and azioni[0]["estratto"] == "Chiamare il commercialista (venerdì)"


def test_model_down_keeps_sources(alice, worker, fake, pulito):  # noqa: F811
    nota(alice, worker, "Il preventivo per il sito costa tremila euro. Lo mando a Luca. Poi vediamo.")
    fake.down = True
    testa, _, righe = chiedi(alice, "Quanto costa il preventivo?")
    assert testa["fonti"] and "errore" in righe[-1]


def test_search_filters_by_branch_and_date(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("cliente", None, "studio")])]
    a = nota(alice, worker, "Il preventivo nuovo per il cliente. Da mandare. Ok.")
    fake.chat_queue = [risposta(argomenti=[("bagno", None, "casa")])]
    b = nota(alice, worker, "Il preventivo del muratore per il bagno. Caro. Vedremo.")
    tutti = {r["entry_id"] for r in alice.get("/api/v1/search?q=preventivo").json()["risultati"]}
    assert {a["id"], b["id"]} <= tutti
    rami = {x["nome"]: x for x in alice.get("/api/v1/rami").json()}
    assert set(rami) == {"studio", "casa"} and rami["studio"]["tinta"] != rami["casa"]["tinta"]
    solo = alice.get(f"/api/v1/search?q=preventivo&ramo={rami['casa']['id']}").json()
    assert [r["entry_id"] for r in solo["risultati"]] == [b["id"]] and solo["risultati"][0]["rami"][0]["nome"] == "casa"
    futuro = alice.get("/api/v1/search", params={"q": "preventivo", "dal": "2030-01-01T00:00:00+02:00"}).json()
    assert futuro["risultati"] == []
