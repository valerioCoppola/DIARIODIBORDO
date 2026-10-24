"""L'albero dei temi che nasce da ciò che si dice: percorsi, domande, risposte, controlli, riordino, colori."""

from __future__ import annotations

import json

import pytest

from app.cli import main as cli
from app.config import get_settings
from app.worker import Worker

from fakes import make_pipeline
from test_conoscenza import fake, nota, pulito, risposta, transcriber, voci, worker  # noqa: F401
from test_pipeline import drain


def grafo(client) -> dict:
    g = client.get("/api/v1/grafo").json()
    g["per_nome"] = {c["nome"]: c for c in g["concetti"]}
    return g


def colloca(*voci_) -> str:
    """Una risposta di collocazione scritta a mano: per ogni argomento (percorso, certezza, alternative)."""
    return json.dumps({"collocazione": [
        {"argomento": i, "percorso": [{"tema": p, "nome": None} if isinstance(p, int) else {"tema": None, "nome": p}
                                      for p in percorso],
         "certezza": certezza, "domanda": None if certezza == "alta" else "Dove va?", "alternative": list(alt)}
        for i, (percorso, certezza, *rest) in enumerate(voci_, 1) for alt in [rest[0] if rest else []]]})


def ultima(fake, chiave: str) -> str:
    return next(r for r in reversed(fake.chat_requests) if chiave in r["format"]["properties"])["messages"][1]["content"]


# -- percorsi ---------------------------------------------------------------------------------

def test_a_path_creates_the_broad_theme_and_never_categories(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("campo A3", None, "BoscoVivo")])]
    nota(alice, worker, "Per il campo A3 serve un sopralluogo. Lo organizzo io. Poi vediamo.")
    g = grafo(alice)
    assert g["per_nome"]["campo A3"]["padre"] == g["per_nome"]["BoscoVivo"]["id"]
    assert g["per_nome"]["BoscoVivo"]["padre"] is None and "aree" not in g        # nessun livello "area"

    # Una categoria al posto della cosa viene rifiutata e chiesta di nuovo.
    fake.chat_queue = [risposta(argomenti=[("orto", None)])]
    fake.colloca_queue = [colloca((["Progetti", "orto"], "alta")), colloca((["orto"], "alta"))]
    nota(alice, worker, "Ho seminato l'orto. Pomodori. Basilico.")
    assert "è una categoria" in fake.chat_requests[-1]["messages"][-1]["content"] or any(
        "è una categoria" in m["content"] for r in fake.chat_requests for m in r["messages"] if m["role"] == "user")
    assert "Progetti" not in grafo(alice)["per_nome"] and grafo(alice)["per_nome"]["orto"]["padre"] is None

    # Il modello vede l'albero senza aree e le indicazioni della persona.
    prompt = ultima(fake, "collocazione")
    assert "1. " in prompt and "BoscoVivo (1 nota)" in prompt and "Area" not in prompt
    assert "Indicazioni date dalla persona: nessuna." in prompt


def test_an_existing_top_theme_can_turn_out_part_of_something_broader(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("Valdirame", None)])]
    nota(alice, worker, "In Valdirame i sensori sono arrivati. Bene. Avanti.")
    fake.chat_queue = [risposta(argomenti=[("Valdirame", None)])]
    fake.colloca_queue = [colloca((["BoscoVivo", 1], "alta"))]        # 1 = Valdirame, ora in cima
    nota(alice, worker, "BoscoVivo: in Valdirame si installa la settimana prossima. Ok. Fine.")
    g = grafo(alice)
    assert g["per_nome"]["Valdirame"]["padre"] == g["per_nome"]["BoscoVivo"]["id"]


def test_existing_steps_must_nest(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("barca", None)])]
    nota(alice, worker, "La barca ha la vela rotta. Va cambiata. Presto.")
    fake.chat_queue = [risposta(argomenti=[("cucina", None)])]
    nota(alice, worker, "In cucina si rompe il rubinetto. Che noia. Domani.")
    fake.chat_queue = [risposta(argomenti=[("vela", None)])]
    numeri = [l for l in ultima(fake, "collocazione").splitlines() if l.strip()[:1].isdigit()]
    assert len(numeri) == 2
    fake.colloca_queue = [colloca(([1, 2, "vela"], "alta")), colloca(([2, "vela"], "alta"))]
    nota(alice, worker, "La vela nuova costa troppo. Cerco usato. Vediamo.")
    assert any("non sta dentro" in m["content"] for r in fake.chat_requests for m in r["messages"] if m["role"] == "user")


# -- incertezza, domande, risposte ----------------------------------------------------------------

def test_uncertain_placement_is_provisional_and_asks(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("BoscoVivo", None)])]
    nota(alice, worker, "BoscoVivo va avanti. Ci sono novità. Bene.")
    fake.chat_queue = [risposta(argomenti=[("sopralluogo L1", "bassa")])]
    e = nota(alice, worker, "Il sopralluogo per L1 è fissato a martedì. Porto il drone. Ok.")
    g = grafo(alice)
    t = g["per_nome"]["sopralluogo L1"]
    assert t["provvisorio"] is True and g["da_chiarire"] == 1
    [c] = e["chiarimenti"]
    assert c["argomento"] == "sopralluogo L1" and c["domanda"] == "Dove va «sopralluogo L1»?"
    etichette = [o["etichetta"] for o in c["opzioni"]]
    assert etichette[0] == "sopralluogo L1"
    assert c["punti"] == ["Si parla di sopralluogo L1"]

    # La persona sceglie: la nota passa lì subito, il percorso resta confermato e diventa un'indicazione.
    [x] = alice.get("/api/v1/chiarimenti").json()
    assert x["id"] == c["id"]
    r = alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"scelta": 0})
    assert r.status_code == 200 and r.json()["applicata"] and r.json()["in_coda"]
    assert alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"scelta": 0}).status_code == 422   # una volta sola
    drain(worker)
    g = grafo(alice)
    assert g["per_nome"]["sopralluogo L1"]["provvisorio"] is False and g["da_chiarire"] == 0
    ind = [i["testo"] for i in alice.get("/api/v1/indicazioni").json()]
    assert "«sopralluogo L1» va in: sopralluogo L1" in ind
    assert alice.get(f"/api/v1/entries/{e['id']}").json()["stato"] == "archiviazione_completata"


def test_free_answer_becomes_an_indication_and_places_again(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("BoscoVivo", None)])]
    nota(alice, worker, "BoscoVivo cresce. Due paesi. Bene.")
    fake.chat_queue = [risposta(argomenti=[("campo A3", "media")])]
    e = nota(alice, worker, "Per il campo A3 mancano i permessi. Li chiedo. Ok.")
    [c] = e["chiarimenti"]
    fake.colloca_queue = [colloca(([1, "Borgoluce", "campo A3"], "alta"))]          # 1 = BoscoVivo
    r = alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"testo": "L1 è il sito in Borgoluce di BoscoVivo"})
    assert r.status_code == 200 and r.json()["ricolloca"]
    drain(worker)
    prompt = ultima(fake, "collocazione")
    assert "- Su «campo A3»: L1 è il sito in Borgoluce di BoscoVivo" in prompt
    g = grafo(alice)
    l1, ec = g["per_nome"]["campo A3"], g["per_nome"]["Borgoluce"]
    assert l1["padre"] == ec["id"] and ec["padre"] == g["per_nome"]["BoscoVivo"]["id"]
    assert l1["provvisorio"] is False
    e = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert e["chiarimenti"] == [] and [b["tema"]["nome"] for b in e["appunti"]] == ["campo A3"]
    assert e["appunti"][0]["punti"][0]["testo"] == "Si parla di campo A3"     # gli appunti non si rifanno


def test_guard_doubts_a_confident_but_incoherent_placement(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("barca", None)], punti=[("fatto", "La vela della barca è rotta")])]
    nota(alice, worker, "La vela della barca è rotta. Va cambiata. Presto.")
    # Il modello è sicurissimo che le fatture dello studio vadano nella barca: i controlli no.
    fake.chat_queue = [risposta(argomenti=[("fatture studio", None)],
                                punti=[("fatto", "Le fatture dello studio sono in ritardo")])]
    fake.colloca_queue = [colloca(([1, "fatture studio"], "alta"))]
    e = nota(alice, worker, "Le fatture dello studio sono in ritardo. Il commercialista aspetta. Uffa.")
    [c] = e["chiarimenti"]
    assert c["opzioni"][0]["etichetta"] == "barca › fatture studio"
    assert any(o["etichetta"] == "fatture studio, a sé" for o in c["opzioni"])
    assert grafo(alice)["per_nome"]["fatture studio"]["provvisorio"] is True
    avvisi = alice.get(f"/api/v1/entries/{e['id']}").json()["analisi"]["avvisi"]
    assert any("somiglia poco a barca" in a for a in avvisi)
    alice.post(f"/api/v1/chiarimenti/{c['id']}",
               json={"scelta": next(o["n"] for o in c["opzioni"] if o["etichetta"] == "fatture studio, a sé")})
    drain(worker)
    g = grafo(alice)
    assert g["per_nome"]["fatture studio"]["padre"] is None and g["per_nome"]["fatture studio"]["provvisorio"] is False


def test_almost_the_same_name_asks_and_learns_the_alias(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("BoscoVivo", None)])]
    nota(alice, worker, "BoscoVivo parte. Primo giorno. Ok.")
    fake.chat_queue = [risposta(argomenti=[("Bosco Viv", None)])]
    e = nota(alice, worker, "Bosco Viv ha un nuovo investitore. Ottimo. Avanti.")
    [c] = e["chiarimenti"]
    assert c["tipo"] == "nome" and c["domanda"] == "«Bosco Viv» è «BoscoVivo»?"
    assert [o["etichetta"] for o in c["opzioni"]] == ["Sì, è BoscoVivo", "No, è un'altra cosa"]
    alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"scelta": 0})
    drain(worker)
    g = grafo(alice)
    assert "Bosco Viv" not in g["per_nome"]                       # il tema provvisorio sparisce
    e = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert [x["nome"] for x in e["concetti"]] == ["BoscoVivo"]
    # Detto di nuovo così, si ritrova da solo: è un alias.
    fake.chat_queue = [risposta(argomenti=[("Bosco Viv", None)])]
    e3 = nota(alice, worker, "Bosco Viv chiude il trimestre. Bene. Fine.")
    assert e3["chiarimenti"] == [] and [x["nome"] for x in e3["concetti"]] == ["BoscoVivo"]


def test_at_most_two_questions_per_note(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[(n, "bassa") for n in ("alfa uno", "beta due", "gamma tre")])]
    e = nota(alice, worker, "Si parla di alfa uno. Poi di beta due. Infine gamma tre.")
    assert len(e["chiarimenti"]) == 2 and all(c["provvisorio"] for c in e["concetti"])


def test_a_likely_placement_with_nothing_to_choose_from_does_not_ask(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("delta", "media")])]
    e = nota(alice, worker, "Di delta. Poi basta. Fine.")
    assert e["chiarimenti"] == [] and e["concetti"][0]["provvisorio"] is True


def test_confirmed_paths_survive_a_rebuild(alice, worker, fake, pulito, capsys):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("BoscoVivo", None)])]
    nota(alice, worker, "BoscoVivo va. Bene. Ok.")
    fake.chat_queue = [risposta(argomenti=[("campo A3", "bassa")])]
    e = nota(alice, worker, "Il campo A3 è pronto. Si parte. Bene.")
    [c] = e["chiarimenti"]
    fake.colloca_queue = [colloca(([1, "Borgoluce", "campo A3"], "alta"))]
    alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"testo": "è in Borgoluce, di BoscoVivo"})
    drain(worker)
    e = alice.get(f"/api/v1/entries/{e['id']}").json()
    punti_prima = [p["testo"] for b in e["appunti"] for p in b["punti"]]

    # Ricostruzione da capo della sola collocazione: gli appunti restano, l'albero si riforma.
    cli(["ricolloca", "--tutte", "--da-capo", "--utente", "alice"])
    assert "In coda: 2 note" in capsys.readouterr().out
    drain(worker)
    e2 = alice.get(f"/api/v1/entries/{e['id']}").json()
    assert [p["testo"] for b in e2["appunti"] for p in b["punti"]] == punti_prima
    g = grafo(alice)
    # …e il percorso deciso a parole non era "confermato": la domanda torna, ma l'indicazione è nel prompt.
    assert "- Su «campo A3»: è in Borgoluce, di BoscoVivo" in ultima(fake, "collocazione")
    assert "BoscoVivo" in g["per_nome"]


def test_chosen_option_is_reapplied_identically_on_rebuild(alice, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("BoscoVivo", None)])]
    nota(alice, worker, "BoscoVivo va. Bene. Ok.")
    fake.chat_queue = [risposta(argomenti=[("Valdirame", "media")])]
    fake.alternative["Valdirame"] = ["BoscoVivo"]
    e = nota(alice, worker, "In Valdirame piove. I lavori si fermano. Peccato.")
    [c] = e["chiarimenti"]
    scelta = next(o["n"] for o in c["opzioni"] if o["etichetta"] == "BoscoVivo › Valdirame")
    alice.post(f"/api/v1/chiarimenti/{c['id']}", json={"scelta": scelta})
    drain(worker)
    g = grafo(alice)
    assert g["per_nome"]["Valdirame"]["padre"] == g["per_nome"]["BoscoVivo"]["id"], g["per_nome"]
    fake.alternative.clear()
    fake.certezze["Valdirame"] = "bassa"                      # da solo, il modello non lo saprebbe
    cli(["ricolloca", "--tutte", "--da-capo", "--utente", "alice"])
    drain(worker)
    g = grafo(alice)
    assert g["per_nome"]["Valdirame"]["padre"] == g["per_nome"]["BoscoVivo"]["id"] and g["da_chiarire"] == 0


# -- riordino ------------------------------------------------------------------------------------

def test_reorder_groups_moves_and_respects_fixed_themes(alice, worker, fake, admin_conn, pulito):  # noqa: F811
    for n in ("Valdirame", "Borgoluce", "orto"):
        fake.chat_queue = [risposta(argomenti=[(n, None)])]
        nota(alice, worker, f"Notizie da {n}. Tutto bene. A presto.")
    g = grafo(alice)
    orto = g["per_nome"]["orto"]
    assert alice.post(f"/api/v1/temi/{orto['id']}/rinomina", json={"nome": "orto di casa"}).status_code == 200
    me = alice.get("/api/v1/auth/me").json()
    uid, diario = me["id"], me["diario"]["id"]

    assert worker.riordina_se_serve(uid, forza=True, diario=diario) == []           # il modello non propone niente
    prompt = ultima(fake, "modifiche")
    numero = {l.split(". ", 1)[1].split(" (")[0]: int(l.strip().split(".")[0])
              for l in prompt.splitlines() if l.strip()[:1].isdigit()}
    assert set(numero) == {"Valdirame", "Borgoluce", "orto di casa"} and "orto di casa (1 nota) — fissato" in prompt
    assert "«orto» si chiama «orto di casa»" in prompt                # le indicazioni arrivano anche qui

    fake.riordino_queue = [json.dumps({"modifiche": [
        {"tipo": "raggruppa", "temi": [numero["Valdirame"], numero["Borgoluce"]], "dentro": None, "nome": "BoscoVivo",
         "motivo": "due paesi dello stesso progetto"},
        {"tipo": "rinomina", "temi": [numero["orto di casa"]], "dentro": None, "nome": "giardino", "motivo": "x"},
        {"tipo": "raggruppa", "temi": [numero["Valdirame"]], "dentro": None, "nome": "Progetti", "motivo": "x"},
        {"tipo": "sposta", "temi": [99], "dentro": None, "nome": None, "motivo": "x"}]})]
    fatto = worker.riordina_se_serve(uid, forza=True, diario=diario)
    assert fatto == ["raggruppati ['Valdirame', 'Borgoluce'] in 'BoscoVivo' (due paesi dello stesso progetto)"]
    g = grafo(alice)
    rt = g["per_nome"]["BoscoVivo"]
    assert g["per_nome"]["Valdirame"]["padre"] == rt["id"] and g["per_nome"]["Borgoluce"]["padre"] == rt["id"]
    assert rt["provvisorio"] is True                              # un raggruppamento automatico è da confermare
    assert "giardino" not in g["per_nome"] and "orto di casa" in g["per_nome"]   # fissato: non si tocca
    assert "Progetti" not in g["per_nome"]
    assert alice.post(f"/api/v1/temi/{rt['id']}/conferma").status_code == 200
    assert grafo(alice)["per_nome"]["BoscoVivo"]["provvisorio"] is False


def test_reorder_runs_every_few_notes(alice, fake, transcriber, pulito):  # noqa: F811
    s = get_settings().model_copy(update={"riordino_ogni": 2, "riordino_ore": 1000})
    w = Worker(make_pipeline(fake, transcriber), settings=s, worker_id="test-riordino")
    drain(w)
    chiamate = []
    for n in ("Valdirame", "Borgoluce", "orto"):
        fake.chat_queue = [risposta(argomenti=[(n, None)])]
        nota(alice, w, f"Notizie da {n}. Tutto bene. A presto.")
        chiamate.append(sum(1 for r in fake.chat_requests if "modifiche" in r["format"]["properties"]))
    # 1ª nota: un tema solo, niente da chiedere al modello; 2ª: una nota dall'ultimo riordino; 3ª: due.
    assert chiamate == [0, 0, 1]


# -- correzioni a mano -----------------------------------------------------------------------------

def test_move_rename_and_merge_by_hand(alice, bruno, worker, fake, pulito):  # noqa: F811
    for n in ("BoscoVivo", "Valdirame", "Bosco Vivo progetto"):
        fake.chat_queue = [risposta(argomenti=[(n, None)])]
        nota(alice, worker, f"Parlo di {n}. Ecco. Fine.")
    g = grafo(alice)
    rt, sn = g["per_nome"]["BoscoVivo"], g["per_nome"]["Valdirame"]
    assert alice.post(f"/api/v1/temi/{sn['id']}/sposta", json={"dentro": rt["id"]}).status_code == 200
    assert alice.post(f"/api/v1/temi/{rt['id']}/sposta", json={"dentro": sn["id"]}).status_code == 422  # niente cicli
    assert bruno.post(f"/api/v1/temi/{sn['id']}/sposta", json={"dentro": None}).status_code == 422
    g = grafo(alice)
    assert g["per_nome"]["Valdirame"]["padre"] == rt["id"] and g["per_nome"]["Valdirame"]["fissato"] is True
    ind = [i["testo"] for i in alice.get("/api/v1/indicazioni").json()]
    assert "«Valdirame» fa parte di «BoscoVivo»" in ind

    # Rinominato con il nome di un tema allo stesso livello: diventano uno.
    altro = g["per_nome"]["Bosco Vivo progetto"]
    r = alice.post(f"/api/v1/temi/{altro['id']}/rinomina", json={"nome": "BoscoVivo"})
    assert r.status_code == 200 and r.json()["id"] == rt["id"]
    g = grafo(alice)
    assert "Bosco Vivo progetto" not in g["per_nome"]
    t = alice.get(f"/api/v1/temi/{rt['id']}").json()
    assert len(t["appunti"]) == 2

    # Un'indicazione sbagliata si toglie.
    i = alice.get("/api/v1/indicazioni").json()[0]
    assert bruno.delete(f"/api/v1/indicazioni/{i['id']}").status_code == 404
    assert alice.delete(f"/api/v1/indicazioni/{i['id']}").status_code == 200


def test_questions_are_private(alice, bruno, worker, fake, pulito):  # noqa: F811
    fake.chat_queue = [risposta(argomenti=[("contenitore", None)])]
    nota(alice, worker, "Il contenitore. Pieno. Ok.")
    fake.chat_queue = [risposta(argomenti=[("dentro", "bassa", "contenitore")])]
    e = nota(alice, worker, "Qualcosa dentro. Forse. Chissà.")
    [c] = e["chiarimenti"]
    assert bruno.get("/api/v1/chiarimenti").json() == []
    assert bruno.post(f"/api/v1/chiarimenti/{c['id']}", json={"scelta": 0}).status_code == 404
    assert alice.post(f"/api/v1/chiarimenti/{c['id']}", json={}).status_code == 422


# -- colori -------------------------------------------------------------------------------------

def test_tints_follow_the_shape_of_the_tree(alice, worker, fake, pulito):  # noqa: F811
    for figlio in ("Valdirame", "Borgoluce", "Kenya", "Perù"):
        fake.chat_queue = [risposta(argomenti=[(figlio, None, "BoscoVivo")])]
        nota(alice, worker, f"Da {figlio}. Bene. Ok.")
    for figlio in ("bagno", "cucina"):
        fake.chat_queue = [risposta(argomenti=[(figlio, None, "casa")])]
        nota(alice, worker, f"La {figlio}. Bene. Ok.")
    g = grafo(alice)
    t = {n: c["tinta"] for n, c in g["per_nome"].items()}

    def dist(a, b):
        d = abs(t[a] - t[b]) % 360
        return min(d, 360 - d)

    assert dist("BoscoVivo", "casa") >= 90                          # i temi in cima: lontani
    paesi = ["Valdirame", "Borgoluce", "Kenya", "Perù"]
    assert len({t[p] for p in paesi}) == 4                          # fratelli: tutti diversi…
    assert min(dist(a, b) for a in paesi for b in paesi if a != b) >= 20
    assert all(dist(p, "BoscoVivo") <= 80 for p in paesi)           # …ma della famiglia del loro tema
    assert dist("bagno", "cucina") >= 20
    # La tinta della prima radice non cambia quando ne nascono altre.
    fake.chat_queue = [risposta(argomenti=[("barca", None)])]
    nota(alice, worker, "La barca. Bene. Ok.")
    assert grafo(alice)["per_nome"]["BoscoVivo"]["tinta"] == t["BoscoVivo"]


@pytest.mark.parametrize("n", [1, 2, 7, 15])
def test_sibling_tints_stay_distinct_for_any_count(n):
    from datetime import datetime, timedelta

    from app.conoscenza import tinte

    class Finta:
        def __init__(self, righe):
            self.righe = righe

        def execute(self, *_a):
            return self

        def fetchall(self):
            return self.righe

    t0 = datetime(2026, 1, 1)
    righe = [{"id": "r", "parent_id": None, "created_at": t0}]
    righe += [{"id": f"f{i}", "parent_id": "r", "created_at": t0 + timedelta(minutes=i)} for i in range(n)]
    t = tinte(Finta(righe))
    figli = sorted(t[f"f{i}"] for i in range(n))
    if n > 1:
        salti = [b - a for a, b in zip(figli, figli[1:])]
        assert min(salti) >= 9                                       # sempre distinguibili
