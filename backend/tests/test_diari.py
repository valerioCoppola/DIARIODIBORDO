"""Più diari per persona e più persone: isolamento, scaffale, inviti, password, amministrazione."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.cli import main as cli
from app.db import user_tx

from conftest import _login
from fakes import audio_with_speech
from test_conoscenza import fake, nota, pulito, risposta, transcriber, voci, worker  # noqa: F401
from test_pipeline import drain, upload

PW = "password-di-prova-789"


def me(c) -> dict:
    return c.get("/api/v1/auth/me").json()


@pytest.fixture
def lavoro(alice):
    """Un secondo diario di alice, eliminato alla fine del test."""
    d = alice.post("/api/v1/diari", json={"nome": "Lavoro", "copertina": "notte"}).json()
    yield d
    alice.post(f"/api/v1/diari/{d['id']}/elimina", json={"conferma": "Lavoro"})


@pytest.fixture(scope="module")
def carla(app, users):
    """Un'amministratrice (alice e bruno restano persone senza ruoli)."""
    cli(["create-user", "carla", "--password", PW, "--admin"])
    c = _login(app, "carla", PW)
    yield c
    c.__exit__(None, None, None)


def _anon(app) -> TestClient:
    c = TestClient(app)
    c.__enter__()
    return c


# -- diari ------------------------------------------------------------------------------------

def test_new_account_has_one_diary_and_the_shelf_lists_it(alice):
    d = alice.get("/api/v1/diari").json()
    assert [x["nome"] for x in d["diari"]] == ["Diario di Bordo"] and d["aperto"] == d["diari"][0]["id"]
    assert me(alice)["diario"]["nome"] == "Diario di Bordo"


def test_without_an_open_diary_only_the_shelf_answers(alice):
    assert alice.post("/api/v1/diari/chiudi").status_code == 200
    assert me(alice)["diario"] is None
    r = alice.get("/api/v1/entries")
    assert r.status_code == 409 and r.json()["errore"] == "nessun_diario"
    assert alice.get("/api/v1/grafo").status_code == 409
    assert alice.get("/api/v1/diari").status_code == 200


def test_diaries_are_separate_worlds(alice, lavoro, worker, fake, pulito):  # noqa: F811
    primo = me(alice)["diario"]["id"]
    fake.chat_queue = [risposta(argomenti=[("orto", None)])]
    n = nota(alice, worker, "Oggi ho seminato i pomodori nell'orto. Domani annaffio. Poi vedremo.")
    assert alice.get("/api/v1/entries").json()["elementi"]

    assert alice.post(f"/api/v1/diari/{lavoro['id']}/apri").json()["nome"] == "Lavoro"
    assert me(alice)["diario"]["id"] == lavoro["id"]
    assert alice.get("/api/v1/entries").json()["elementi"] == []
    assert alice.get(f"/api/v1/entries/{n['id']}").status_code == 404
    assert alice.get("/api/v1/grafo").json()["concetti"] == []
    assert alice.get("/api/v1/search", params={"q": "pomodori"}).json()["risultati"] == []

    # Il primo diario resta lì, intatto.
    alice.post(f"/api/v1/diari/{primo}/apri")
    assert alice.get(f"/api/v1/entries/{n['id']}").status_code == 200
    # Lo scaffale conta le note di ciascuno, l'ultimo aperto in testa.
    sc = {d["nome"]: d for d in alice.get("/api/v1/diari").json()["diari"]}
    assert sc["Diario di Bordo"]["note"] == 1 and sc["Lavoro"]["note"] == 0
    assert alice.get("/api/v1/diari").json()["diari"][0]["nome"] == "Diario di Bordo"


def test_a_recording_goes_to_the_diary_it_was_made_in(alice, lavoro, worker, fake, pulito):  # noqa: F811
    """Il telefono indica il diario del vocale (X-Diario): ci arriva anche se intanto se ne è aperto un altro."""
    fake.chat_queue = [risposta(argomenti=[("cliente Rossi", None)])]
    r = alice.post("/api/v1/recordings", headers={"X-Diario": lavoro["id"]}, data={"source": "registratore"},
                   files={"file": ("v.webm", audio_with_speech("Chiamare il cliente Rossi. Bene. Ok."), "audio/webm")})
    assert r.status_code == 201
    drain(worker)
    assert alice.get("/api/v1/entries").json()["elementi"] == []          # non nel diario aperto
    elenco = alice.get("/api/v1/entries", headers={"X-Diario": lavoro["id"]}).json()["elementi"]
    assert len(elenco) == 1
    temi = alice.get("/api/v1/grafo", headers={"X-Diario": lavoro["id"]}).json()["concetti"]
    assert [t["nome"] for t in temi] == ["cliente Rossi"]                  # la conoscenza cresce lì
    assert alice.get("/api/v1/grafo").json()["concetti"] == []


def test_someone_elses_diary_cannot_be_used(alice, bruno):
    di_bruno = me(bruno)["diario"]["id"]
    assert alice.get("/api/v1/entries", headers={"X-Diario": di_bruno}).status_code == 404
    assert alice.post(f"/api/v1/diari/{di_bruno}/apri").status_code == 404
    assert alice.patch(f"/api/v1/diari/{di_bruno}", json={"nome": "mio"}).status_code == 404
    assert alice.post(f"/api/v1/diari/{di_bruno}/elimina", json={"conferma": "Diario di Bordo"}).status_code == 404
    assert alice.get("/api/v1/entries", headers={"X-Diario": "non-un-id"}).status_code == 400


def test_rename_and_cover(alice, lavoro):
    r = alice.patch(f"/api/v1/diari/{lavoro['id']}", json={"nome": "  Lavoro  ", "copertina": "bosco"})
    assert r.json()["nome"] == "Lavoro" and r.json()["copertina"] == "bosco"
    assert alice.patch(f"/api/v1/diari/{lavoro['id']}", json={"copertina": "fucsia"}).status_code == 422
    assert alice.post("/api/v1/diari", json={"nome": "   "}).status_code == 422


def test_the_same_audio_can_live_in_two_diaries(alice, lavoro, admin_conn, pulito):  # noqa: F811
    a = upload(alice, "Lo stesso audio in due diari. Una frase. Due frasi.")
    b = alice.post("/api/v1/recordings", headers={"X-Diario": lavoro["id"]}, data={"source": "caricamento"},
                   files={"file": ("v.webm", audio_with_speech("Lo stesso audio in due diari. Una frase. Due frasi."),
                                   "audio/webm")}).json()
    assert not a["duplicato"] and not b["duplicato"] and a["recording_id"] != b["recording_id"]
    paths = dict(admin_conn.execute("SELECT diario_id, storage_path FROM recordings WHERE id IN (%s, %s)",
                                    (a["recording_id"], b["recording_id"])).fetchall())
    assert len(set(paths.values())) == 2 and all(str(k) in v for k, v in paths.items())


def test_deleting_a_diary_takes_everything_it_contains(alice, worker, fake, admin_conn, pulito):  # noqa: F811
    from app import storage

    d = alice.post("/api/v1/diari", json={"nome": "Viaggio"}).json()
    alice.post(f"/api/v1/diari/{d['id']}/apri")
    fake.chat_queue = [risposta(argomenti=[("Islanda", None)], azioni=[("prenotare il traghetto", None, None)])]
    n = nota(alice, worker, "Per l'Islanda devo prenotare il traghetto. Sarà bello. Partiamo a luglio.")
    rel = admin_conn.execute("SELECT storage_path FROM recordings WHERE id = %s", (n["recording_id"],)).fetchone()[0]
    file = storage.absolute_path(rel)
    assert file.exists()

    r = alice.post(f"/api/v1/diari/{d['id']}/elimina", json={"conferma": "viaggio?"})
    assert r.status_code == 422 and r.json()["errore"] == "conferma_errata"
    assert alice.post(f"/api/v1/diari/{d['id']}/elimina", json={"conferma": "viaggio"}).status_code == 200
    assert not file.exists()
    for t in ("recordings", "entries", "topics", "action_items", "punti", "text_chunks", "ingestion_jobs"):
        assert admin_conn.execute(f"SELECT count(*) FROM {t} WHERE diario_id = %s", (d["id"],)).fetchone()[0] == 0
    # La sessione era dentro quel diario: ora è sullo scaffale.
    assert me(alice)["diario"] is None
    assert alice.get("/api/v1/entries").status_code == 409


def test_the_last_diary_cannot_be_deleted(bruno):
    solo = me(bruno)["diario"]
    r = bruno.post(f"/api/v1/diari/{solo['id']}/elimina", json={"conferma": solo["nome"]})
    assert r.status_code == 409 and r.json()["errore"] == "ultimo_diario"


def test_rows_cannot_be_written_into_another_diary(alice, lavoro):
    """Anche con un errore nel codice: la politica del diario rifiuta righe fuori dal diario della transazione."""
    import psycopg

    m = me(alice)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(m["id"], m["diario"]["id"]) as c:
            c.execute("INSERT INTO indicazioni (owner_id, diario_id, testo) VALUES (%s, %s, 'x')",
                      (m["id"], lavoro["id"]))
    with user_tx(m["id"]) as c:   # senza diario: nessun contenuto visibile
        assert c.execute("SELECT count(*) AS n FROM recordings").fetchone()["n"] == 0


# -- persone ----------------------------------------------------------------------------------

def test_only_the_admin_manages_accounts(alice, carla):
    assert alice.get("/api/v1/admin/utenti").status_code == 403
    assert alice.post("/api/v1/admin/inviti", json={}).status_code == 403
    assert me(carla)["amministratore"] and not me(alice)["amministratore"]
    nomi = {u["username"] for u in carla.get("/api/v1/admin/utenti").json()["utenti"]}
    assert {"alice", "bruno", "carla"} <= nomi


def test_an_invitation_creates_one_account(app, carla):
    inv = carla.post("/api/v1/admin/inviti", json={"nota": "per Dora"}).json()
    anon = _anon(app)
    r = anon.post("/api/v1/auth/registrati", json={"codice": "XXXX-YYYY-ZZZZ", "username": "dora", "password": PW})
    assert r.status_code == 403
    r = anon.post("/api/v1/auth/registrati", json={"codice": inv["codice"], "username": "dora", "password": "corta"})
    assert r.status_code == 422 and r.json()["errore"] == "password_corta"
    # Il codice si può scrivere anche in minuscolo e senza trattini.
    r = anon.post("/api/v1/auth/registrati", json={"codice": inv["codice"].replace("-", "").lower(),
                                                   "username": "Dora", "password": PW})
    assert r.status_code == 200, r.text
    anon.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    m = me(anon)
    assert m["username"] == "dora" and not m["amministratore"] and m["diario"] is None     # sullo scaffale
    assert [d["nome"] for d in anon.get("/api/v1/diari").json()["diari"]] == ["Diario di Bordo"]
    # Usato: non vale più.
    again = _anon(app).post("/api/v1/auth/registrati", json={"codice": inv["codice"], "username": "dora2",
                                                             "password": PW})
    assert again.status_code == 403
    stato = {i["nota"]: i for i in carla.get("/api/v1/admin/inviti").json()["inviti"]}["per Dora"]
    assert stato["stato"] == "usato" and stato["usato_da"] == "dora"


def test_a_revoked_invitation_is_refused(app, carla):
    inv = carla.post("/api/v1/admin/inviti", json={"nota": "ripensamento"}).json()
    iid = next(i["id"] for i in carla.get("/api/v1/admin/inviti").json()["inviti"] if i["nota"] == "ripensamento")
    carla.delete(f"/api/v1/admin/inviti/{iid}")
    r = _anon(app).post("/api/v1/auth/registrati", json={"codice": inv["codice"], "username": "ezio", "password": PW})
    assert r.status_code == 403


def test_temporary_password_must_be_changed(app, carla):
    cli(["create-user", "fabio", "--password", PW])
    fabio = _login(app, "fabio", PW)
    fid = me(fabio)["id"]
    tmp = carla.post(f"/api/v1/admin/utenti/{fid}/password").json()["password"]
    assert fabio.get("/api/v1/auth/me").status_code == 401                 # sessioni chiuse
    c = _anon(app)
    r = c.post("/api/v1/auth/login", json={"username": "fabio", "password": tmp})
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert me(c)["cambia_password"]
    assert c.get("/api/v1/diari").status_code == 403
    assert c.post("/api/v1/auth/password", json={"attuale": "sbagliata", "nuova": PW + "x"}).status_code == 403
    assert c.post("/api/v1/auth/password", json={"attuale": tmp, "nuova": PW + "x"}).status_code == 200
    assert not me(c)["cambia_password"] and c.get("/api/v1/diari").status_code == 200


def test_changing_password_closes_the_other_sessions(app):
    cli(["create-user", "gina", "--password", PW])
    a, b = _login(app, "gina", PW), _login(app, "gina", PW)
    assert a.post("/api/v1/auth/password", json={"attuale": PW, "nuova": PW + "!"}).status_code == 200
    assert a.get("/api/v1/auth/me").status_code == 200 and b.get("/api/v1/auth/me").status_code == 401


def test_a_disabled_account_cannot_get_in(app, carla):
    cli(["create-user", "ugo", "--password", PW])
    ugo = _login(app, "ugo", PW)
    uid = me(ugo)["id"]
    assert carla.post(f"/api/v1/admin/utenti/{me(carla)['id']}/disattiva").status_code == 409   # non sé stessa
    assert carla.post(f"/api/v1/admin/utenti/{uid}/disattiva").status_code == 200
    assert ugo.get("/api/v1/auth/me").status_code == 401
    assert _anon(app).post("/api/v1/auth/login", json={"username": "ugo", "password": PW}).status_code == 401
    carla.post(f"/api/v1/admin/utenti/{uid}/attiva")
    assert _anon(app).post("/api/v1/auth/login", json={"username": "ugo", "password": PW}).status_code == 200


def test_admin_cli(capsys):
    cli(["invito", "--nota", "da riga di comando"])
    out = capsys.readouterr().out
    assert "Codice d'invito" in out
    cli(["utenti"])
    assert "carla" in capsys.readouterr().out
