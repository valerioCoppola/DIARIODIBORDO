"""Schema canonico della nota (spec. §3) e migrazioni."""

import pytest
from pydantic import ValidationError

from app.note_schema import Nota, soft_warnings

VALIDA = {
    "titolo": "Pianificazione settimana progetto Alfa",
    "data_ora": "2026-10-21T08:30:00+02:00",
    "categoria": "Lavoro",
    "tag": ["progetto alfa", "pianificazione"],
    "executive_summary": ["Uno.", "Due.", "Tre."],
    "action_items": [{"descrizione": "Inviare il preventivo", "responsabile": None, "scadenza": None}],
    "trascrizione_grezza": "Oggi  ho pensato... al progetto Alfa.\n\nE poi basta.",
}


def test_valid_note():
    n = Nota.model_validate(VALIDA)
    assert n.trascrizione_grezza == VALIDA["trascrizione_grezza"]  # nessuna riscrittura
    assert soft_warnings(n) == []


def test_minimal_note_defaults():
    n = Nota.model_validate({"titolo": "Una nota senza data", "executive_summary": ["a", "b", "c"],
                             "trascrizione_grezza": "testo"})
    assert n.data_ora is None and n.categoria.value == "Altro" and n.tag == [] and n.action_items == []


@pytest.mark.parametrize("patch", [
    {"executive_summary": ["a", "b"]},
    {"executive_summary": ["a", "b", "c", "d"]},
    {"executive_summary": ["a", " ", "c"]},
    {"categoria": "Lavori"},
    {"tag": ["a", "b", "c", "d", "e", "f"]},
    {"data_ora": "2026-10-21T08:30:00"},          # manca l'offset
    {"trascrizione_grezza": ""},
    {"action_items": [{"descrizione": "x", "priorita": "alta"}]},
    {"campo_extra": 1},
])
def test_invalid_notes(patch):
    with pytest.raises(ValidationError):
        Nota.model_validate({**VALIDA, **patch})


def test_title_length_is_a_warning():
    n = Nota.model_validate({**VALIDA, "titolo": "Breve"})
    assert soft_warnings(n) and "3–6" in soft_warnings(n)[0]


def test_schema_endpoint(anon):
    s = anon.get("/api/v1/note-schema").json()
    assert set(s["required"]) >= {"titolo", "executive_summary", "trascrizione_grezza"}


def test_migrations_are_idempotent_and_tracked():
    from app.db import run_migrations
    assert run_migrations() == []


def test_all_spec_tables_exist(admin_conn):
    expected = {"recordings", "entries", "entry_versions", "topics", "entry_topics", "entities",
                "entity_mentions", "entity_relations", "decisions", "action_items", "action_item_events",
                "text_chunks", "ingestion_jobs", "job_events", "upload_sessions", "model_usage"}
    rows = admin_conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    assert expected <= {r[0] for r in rows}


def test_vector_and_fulltext_search_work(admin_conn):
    """pgvector e la ricerca testuale italiana funzionano sullo schema."""
    with admin_conn.transaction():
        uid = admin_conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
        eid = admin_conn.execute(
            "INSERT INTO entries (owner_id, diario_id, titolo, trascrizione_grezza, source) "
            "VALUES (%s, (SELECT id FROM diari WHERE owner_id = %s LIMIT 1), 'Riunione sul budget', "
            "'Abbiamo deciso di rimandare le assunzioni', 'api') RETURNING id",
            (uid, uid)).fetchone()[0]
        vec = "[" + ",".join(["0.1"] * 1024) + "]"
        admin_conn.execute(
            "INSERT INTO text_chunks (owner_id, diario_id, entry_id, ord, char_start, char_end, content, embedding, "
            "embedding_model) SELECT owner_id, diario_id, id, 0, 0, 41, 'Abbiamo deciso di rimandare le assunzioni', "
            "%s, 'test' FROM entries WHERE id = %s",
            (vec, eid))
        hit = admin_conn.execute(
            "SELECT titolo FROM entries WHERE search_tsv @@ plainto_tsquery('italian', 'rimandato assunzione')"
        ).fetchone()
        assert hit is not None  # stemming italiano: rimandare/rimandato, assunzioni/assunzione
        dist = admin_conn.execute("SELECT embedding <=> %s::vector FROM text_chunks WHERE entry_id = %s",
                                  (vec, eid)).fetchone()[0]
        assert dist == pytest.approx(0.0, abs=1e-6)
        admin_conn.execute("DELETE FROM entries WHERE id = %s", (eid,))


def test_entry_summary_must_have_three_items(admin_conn):
    import psycopg
    uid = admin_conn.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        admin_conn.execute("INSERT INTO entries (owner_id, diario_id, source, executive_summary) "
                           "VALUES (%s, (SELECT id FROM diari WHERE owner_id = %s LIMIT 1), 'api', %s)",
                           (uid, uid, ["a", "b"]))
