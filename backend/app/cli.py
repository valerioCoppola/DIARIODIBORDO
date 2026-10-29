"""Comandi amministrativi.

    python -m app.cli migrate
    python -m app.cli create-user NOME [--admin]
    python -m app.cli utenti                   persone, ruoli, ultimo accesso
    python -m app.cli invito [--nota "per Marta"] [--giorni 7]
                                               codice monouso per crearsi un account
    python -m app.cli disattiva NOME | riattiva NOME | password-provvisoria NOME
    python -m app.cli rendi-admin NOME [--togli]
    python -m app.cli diari NOME               i diari di una persona
    python -m app.cli elimina-utente NOME      account e tutti i suoi diari (chiede conferma)
    python -m app.cli create-token NOME "comandi-rapidi"
    python -m app.cli verify-storage NOME
    python -m app.cli cleanup-uploads NOME
    python -m app.cli prepara-modelli          scarica i modelli di trascrizione, analisi ed embedding
    python -m app.cli diagnosi                 verifica GPU, modelli, Ollama, worker
    python -m app.cli riprova-errori NOME      rimette in coda i vocali fermi per errore
    python -m app.cli rianalizza --tutte [--da-capo] [--voci]
                                               rifà l'analisi partendo dalle trascrizioni salvate
    python -m app.cli ricolloca --tutte [--da-capo]
                                               rifà solo la collocazione nell'albero dei temi
    python -m app.cli riordina                 riordina subito l'albero dei temi
    python -m app.cli scegli-modello           sceglie il modello di analisi che sta nella scheda video
"""

from __future__ import annotations

import argparse
import getpass
import sys
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import account, ingest, storage
from .account import AccountError
from .db import close_pool, run_migrations, system_tx, user_tx
from .security import create_api_token


def _user_id(username: str):
    with system_tx() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = %s", (username.lower(),)).fetchone()
    if row is None:
        sys.exit(f"Utente inesistente: {username}")
    return row["id"]


def _diari(uid, nome: str | None = None) -> list[dict]:
    """I diari su cui lavorare: quello indicato per nome, altrimenti tutti."""
    with user_tx(uid) as conn:
        rows = conn.execute("SELECT id, nome FROM diari ORDER BY created_at").fetchall()
    if nome is None:
        return rows
    scelti = [r for r in rows if r["nome"].casefold() == nome.strip().casefold()]
    if not scelti:
        sys.exit(f"Diario inesistente: {nome} (ci sono: {', '.join(r['nome'] for r in rows)})")
    return scelti


def _intestazione(diari: list[dict], d: dict) -> None:
    if len(diari) > 1:
        print(f"— {d['nome']}")


def _note_per_diario(uid, diari: list[dict], prefissi: list[str]) -> dict:
    """In quale diario sta ogni nota indicata con l'inizio del suo id: {diario: [prefissi]}."""
    out: dict = {}
    for pref in prefissi:
        trovate = []
        for d in diari:
            with user_tx(uid, d["id"]) as conn:
                trovate += [d["id"]] * len(conn.execute(
                    "SELECT 1 FROM entries WHERE deleted_at IS NULL AND trascrizione_grezza IS NOT NULL "
                    "AND id::text LIKE %s", (pref.lower() + "%",)).fetchall())
        if len(trovate) != 1:
            sys.exit(f"{pref}: {'nessuna nota' if not trovate else 'più note'} con questo id")
        out.setdefault(trovate[0], []).append(pref)
    return out


def cmd_migrate(_: argparse.Namespace) -> None:
    applied = run_migrations()
    print("Migrazioni applicate: " + (", ".join(applied) if applied else "nessuna (schema aggiornato)"))


def cmd_create_user(a: argparse.Namespace) -> None:
    password = a.password or getpass.getpass("Password (min 12 caratteri): ")
    if len(password) < 12:
        sys.exit("Password troppo corta: almeno 12 caratteri")
    if not a.password and getpass.getpass("Ripeti password: ") != password:
        sys.exit("Le password non coincidono")
    try:
        with system_tx() as conn:
            exists = conn.execute("SELECT id FROM users WHERE username = %s", (a.username.lower(),)).fetchone()
            if exists and not a.reset:
                sys.exit("Utente già esistente (usa --reset per cambiare la password)")
            if exists:
                account.imposta_password(conn, exists["id"], password, provvisoria=False)
                print("Password aggiornata, sessioni esistenti revocate.")
            else:
                account.crea_utente(conn, a.username, password, is_admin=a.admin)
                print(f"Utente creato: {a.username.lower()}" + (" (amministratore)" if a.admin else "")
                      + f", con il diario «{account.PRIMO_DIARIO}»")
    except AccountError as e:
        sys.exit(e.message)


def cmd_utenti(_: argparse.Namespace) -> None:
    fmt = "%d/%m/%Y %H:%M"
    for u in account.elenco_utenti():
        stato = ("disattivato" if u["disabled"] else "password provvisoria" if u["cambia_password"] else "attivo")
        visto = u["visto_il"] or u["last_login_at"]
        print(f"  {u['username']:<20} {'amministratore' if u['is_admin'] else 'persona':<15} {stato:<21} "
              f"ultimo accesso: {visto.astimezone().strftime(fmt) if visto else 'mai'}")


def cmd_invito(a: argparse.Namespace) -> None:
    codice, scade = account.crea_invito(None, a.nota, a.giorni)
    print(f"Codice d'invito (vale una volta, fino al {scade.astimezone().strftime('%d/%m/%Y %H:%M')}):\n")
    print(f"    {codice}\n")
    print("Chi lo riceve apre il Diario, tocca «Hai un invito? Crea il tuo account» e sceglie nome utente e password.")


def cmd_attivo(a: argparse.Namespace) -> None:
    uid = _user_id(a.username)
    try:
        account.imposta_attivo(None, uid, a.attivo)
    except AccountError as e:
        sys.exit(e.message)
    print(f"{a.username}: {'riattivato' if a.attivo else 'disattivato, sessioni e token revocati'}")


def cmd_password_provvisoria(a: argparse.Namespace) -> None:
    nuova = account.reimposta_password(None, _user_id(a.username))
    print(f"Password provvisoria per {a.username} (al primo accesso ne sceglie una sua):\n\n    {nuova}\n")


def cmd_rendi_admin(a: argparse.Namespace) -> None:
    uid = _user_id(a.username)
    with system_tx() as conn:
        if a.togli and conn.execute("SELECT count(*) AS n FROM users WHERE is_admin AND id <> %s AND NOT disabled",
                                    (uid,)).fetchone()["n"] == 0:
            sys.exit("È l'unico amministratore attivo: prima rendi amministratore qualcun altro")
        conn.execute("UPDATE users SET is_admin = %s WHERE id = %s", (not a.togli, uid))
    print(f"{a.username}: {'non più amministratore' if a.togli else 'amministratore'}")


def cmd_diari(a: argparse.Namespace) -> None:
    for d in account.elenco_diari(_user_id(a.username)):
        print(f"  {d['nome']:<40} {d['note']:>5} note   id {d['id']}")


def cmd_elimina_utente(a: argparse.Namespace) -> None:
    from .playback import remove_files

    uid = _user_id(a.username)
    diari = account.elenco_diari(uid)
    note = sum(d["note"] for d in diari)
    print(f"Elimina {a.username} con {len(diari)} diari e {note} note, audio compresi. Non si può annullare.")
    if input(f"Per confermare scrivi il nome utente ({a.username}): ").strip().lower() != a.username.lower():
        sys.exit("Annullato")
    paths = []
    for d in diari:
        with user_tx(uid, d["id"]) as conn:
            for r in conn.execute("SELECT storage_path FROM recordings").fetchall():
                try:
                    paths.append(storage.absolute_path(r["storage_path"]))
                except ValueError:
                    pass
    with system_tx() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (uid,))     # tutto il resto segue (ON DELETE CASCADE)
    for path in paths:
        remove_files(path)
    print(f"Eliminato. File audio cancellati: {len(paths)}")


def cmd_create_token(a: argparse.Namespace) -> None:
    token = create_api_token(_user_id(a.username), a.name)
    print("Token API (mostrato una sola volta, conservalo in un gestore di password):")
    print(token)


def cmd_verify_storage(a: argparse.Namespace) -> None:
    """Ricalcola lo sha256 di ogni originale e aggiorna lo stato di conservazione."""
    uid = _user_id(a.username)
    rows = []
    for d in _diari(uid):
        with user_tx(uid, d["id"]) as conn:
            rows += [{**r, "diario": d["id"]} for r in conn.execute(
                "SELECT id, storage_path, sha256 FROM recordings WHERE storage_status <> 'eliminato'").fetchall()]
    bad = 0
    for r in rows:
        path = storage.absolute_path(r["storage_path"])
        if not path.exists():
            state = "mancante"
        elif storage.sha256_file(path) != bytes(r["sha256"]).hex():
            state = "corrotto"
        else:
            state = "verificato"
        bad += state != "verificato"
        with user_tx(uid, r["diario"]) as conn:
            conn.execute("UPDATE recordings SET storage_status = %s, last_verified_at = now() WHERE id = %s",
                         (state, r["id"]))
        if state != "verificato":
            print(f"{r['id']}: {state}")
    print(f"Verificate {len(rows)} registrazioni, problemi: {bad}")
    if bad:
        sys.exit(1)


def cmd_cleanup_uploads(a: argparse.Namespace) -> None:
    uid = _user_id(a.username)
    n = sum(ingest.cleanup_expired_uploads(uid, d["id"]) for d in _diari(uid))
    print(f"Sessioni di caricamento scadute rimosse: {n}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _ko(msg: str) -> None:
    print(f"  ✗ {msg}")


def cmd_prepara_modelli(_: argparse.Namespace) -> None:
    """Scarica tutto ciò che serve, una volta sola: poi l'elaborazione è offline."""
    from .config import get_settings
    from .pipeline.ollama import OllamaClient
    from .pipeline.transcribe import FasterWhisperTranscriber

    s = get_settings()
    t = FasterWhisperTranscriber(s)
    c = t.choice
    print(f"Trascrizione: faster-whisper {c.model} su {c.device} ({c.compute_type})")
    from faster_whisper.utils import download_model

    print("  scaricamento del modello (circa 1,5–3 GB la prima volta)…", flush=True)
    path = download_model(c.model)
    _ok(f"modello in {path}")
    client = OllamaClient(s.ollama_url, timeout=60)
    for m in (s.ollama_model, s.ollama_embed_model):
        print(f"Ollama: {m} (può richiedere qualche minuto)…", flush=True)
        try:
            client.pull(m)
        except Exception as e:
            sys.exit(f"  ✗ impossibile scaricare {m}: {e}\n  Ollama è installato e avviato? (ollama --version)")
        _ok(f"{m} pronto")


def cmd_diagnosi(a: argparse.Namespace) -> None:
    """Controlla ogni componente e dice cosa sistemare. Esce con 1 se qualcosa non va."""
    import json
    import time

    from .config import get_settings
    from .pipeline.analyze import RIORGANIZZA_SCHEMA
    from .pipeline.errors import PipelineError
    from .pipeline.ollama import OllamaClient
    from .pipeline.gpu import main_gpu
    from .pipeline.transcribe import FasterWhisperTranscriber, cuda_device_count

    s = get_settings()
    bad = 0
    print("GPU")
    g, cuda = main_gpu(), cuda_device_count()
    if g:
        motore = "CUDA" if g.vendor == "NVIDIA" else "ROCm"
        _ok(f"{g.vendor} {g.name}, {g.vram_mb} MB di memoria video; vista da ctranslate2 ({motore}): "
            f"{'sì' if cuda else 'NO'}")
        if not cuda:
            bad += 1
            _ko(f"ctranslate2 non vede la GPU: rilancia scripts/prepara-ai.sh (installa la versione per {motore})")
    else:
        print("  - nessuna GPU NVIDIA o AMD: la trascrizione userà la CPU (più lenta)")

    print("Trascrizione")
    t = FasterWhisperTranscriber(s)
    c = t.choice
    _ok(f"scelta: faster-whisper {c.model} su {c.device} ({c.compute_type})")
    if not a.veloce:
        import tempfile
        import wave

        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with wave.open(f.name, "wb") as w:   # 2 secondi di silenzio: basta per caricare e provare
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 32000)
            t0 = time.monotonic()
            try:
                r = t.transcribe(f.name)
                _ok(f"modello caricato ed eseguito su {r.device} in {time.monotonic() - t0:.1f} s")
                if r.device != c.device:
                    bad += 1
                    _ko(f"la GPU non ha funzionato ed è stata usata la {r.device}: vedi i log del worker")
            except PipelineError as e:
                bad += 1
                _ko(str(e))

    print("Ollama")
    client = OllamaClient(s.ollama_url, timeout=300, embed_cpu=s.embedding_su_cpu)
    try:
        _ok(f"versione {client.version()} su {s.ollama_url}")
        have = client.installed_models()
        for m in (s.ollama_model, s.ollama_embed_model):
            present = any(x == m or x == f"{m}:latest" for x in have)
            if present:
                _ok(f"modello {m} installato")
            else:
                bad += 1
                _ko(f"modello {m} mancante: ollama pull {m}")
        if not a.veloce:
            t0 = time.monotonic()
            res = client.chat_json(s.ollama_model, [
                {"role": "system", "content": "Compila la scheda in JSON usando solo il testo."},
                {"role": "user", "content": "Trascrizione:\n<<<\nDomani devo portare la macchina dal meccanico. "
                                            "Poi passo in banca. La sera cena da mia sorella.\n>>>"}],
                RIORGANIZZA_SCHEMA, s.analysis_max_context)
            json.loads(res.content)
            _ok(f"analisi di prova in JSON valido in {time.monotonic() - t0:.1f} s")
            info = next((x for x in client.loaded() if x.get("name") in (s.ollama_model, f"{s.ollama_model}:latest")),
                        None)
            if info and info.get("size"):
                size, vr = info["size"], info.get("size_vram", 0)
                if vr >= size * 0.99:
                    _ok(f"memoria: {size / 1024**3:.1f} GB, tutto sulla scheda video")
                else:
                    _ko(f"memoria: {(size - vr) / 1024**3:.1f} GB di {size / 1024**3:.1f} nella RAM (più lento): "
                        "scegli un modello più piccolo con  scripts/prepara-ai.sh")
            e = client.embed(s.ollama_embed_model, ["prova"])
            dims = len(e.vectors[0])
            if dims == s.embedding_dimensions:
                _ok(f"embedding a {dims} dimensioni")
            else:
                bad += 1
                _ko(f"embedding a {dims} dimensioni, lo schema ne vuole {s.embedding_dimensions}: usa bge-m3")
    except PipelineError as e:
        bad += 1
        _ko(f"{e} — installa e avvia Ollama: scripts/prepara-ai.sh")

    print("Voci")
    import subprocess

    from .voci import SCRIPT, Diarizzatore

    if Diarizzatore(s).disponibile():
        try:
            p = subprocess.run([str(s.voci_python), str(SCRIPT), "--verifica"], capture_output=True, text=True,
                               timeout=300)
            v = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None
        except Exception:
            v = None
        if v and v.get("errore_gpu"):
            bad += 1
            _ko(f"pyannote: la scheda video non funziona ({v['errore_gpu']}), userebbe la CPU: "
                "rilancia scripts/prepara-ai.sh")
        elif v:
            dove = f"scheda video ({v.get('motore', 'CUDA')})" if v["cuda"] else "solo CPU: più lento"
            _ok(f"pyannote pronto (torch {v['torch']}, {dove})")
        else:
            bad += 1
            _ko("l'ambiente delle voci non si avvia: rilancia scripts/prepara-ai.sh")
    else:
        print("  - riconoscimento di chi parla non preparato: scripts/prepara-ai.sh")

    print("Elaborazione")
    with system_tx() as conn:
        w = conn.execute("SELECT worker_id, now() - last_seen_at < interval '90 seconds' AS attivo FROM "
                         "worker_heartbeats ORDER BY last_seen_at DESC LIMIT 1").fetchone()
    if w and w["attivo"]:
        _ok(f"worker attivo ({w['worker_id']})")
    else:
        bad += 1
        _ko("worker non attivo: systemctl --user status diario-worker (o scripts/installa-servizio.sh)")
    print("\nTutto a posto." if not bad else f"\nProblemi trovati: {bad}")
    if bad:
        sys.exit(1)


def _utente(username: str | None):
    if username:
        return _user_id(username)
    with system_tx() as conn:
        rows = conn.execute("SELECT id, username FROM users ORDER BY created_at").fetchall()
    if len(rows) != 1:
        sys.exit("Più utenti: indica quale con --utente NOME (" + ", ".join(r["username"] for r in rows) + ")")
    return rows[0]["id"]


def _rianalizza_diario(a: argparse.Namespace, uid, diario, prefissi: list[str]) -> tuple[int, int, int]:
    from .conoscenza import pulisci_orfani
    from .correzione import FERMI

    with user_tx(uid, diario) as conn:
        q = """SELECT e.id, e.titolo, e.recording_id, r.storage_path,
                      EXISTS (SELECT 1 FROM entry_versions v WHERE v.entry_id = e.id
                               AND v.origin = 'correzione_manuale') AS corretta
                 FROM entries e LEFT JOIN recordings r ON r.id = e.recording_id
                WHERE e.deleted_at IS NULL AND e.trascrizione_grezza IS NOT NULL"""
        glossario = ""
        if a.ritrascrivi:
            from .evoluzione import glossario as glossario_archivio

            glossario = glossario_archivio(conn)   # prima di --da-capo, che toglie i temi
        if a.tutte:
            entries = conn.execute(q + " ORDER BY coalesce(e.data_ora, e.created_at)").fetchall()
        else:
            entries = []
            for pref in prefissi:
                entries += conn.execute(q + " AND e.id::text LIKE %s", (pref.lower() + "%",)).fetchall()
        if a.da_capo:
            from .evoluzione import azzera

            azzera(conn)
            pulisci_orfani(conn)
            print("Albero dei temi e schede cancellati: si riformano nota dopo nota, dalla più vecchia.")
        in_coda = saltate = ritrascritte = 0
        for i, e in enumerate(entries):
            job = conn.execute(
                """SELECT id, status::text AS stato, kind,
                          (locked_at IS NOT NULL AND locked_at > now() - interval '10 minutes') AS occupato
                     FROM ingestion_jobs WHERE entry_id = %s OR (recording_id IS NOT NULL AND recording_id = %s)
                    ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (e["id"], e["recording_id"])).fetchone()
            if job is not None and (job["stato"] not in FERMI or job["occupato"]):
                print(f"  saltata (in elaborazione): {e['titolo'] or e['id']}")
                saltate += 1
                continue
            ritrascrivi = False
            if a.ritrascrivi:
                if e["corretta"] and not a.anche_corrette:
                    print(f"  solo rianalisi (trascrizione corretta a mano): {e['titolo'] or e['id']}")
                elif not e["recording_id"] or not storage.absolute_path(e["storage_path"]).is_file():
                    print(f"  solo rianalisi (audio non più sul PC): {e['titolo'] or e['id']}")
                else:
                    ritrascrivi = True
            if a.voci and not ritrascrivi:
                conn.execute(
                    """UPDATE transcriptions SET voci = %s WHERE id = (SELECT id FROM transcriptions
                        WHERE entry_id = %s ORDER BY created_at DESC LIMIT 1)""",
                    (Jsonb({"persone": a.persone}) if a.persone else None, e["id"]))
            # In ordine di data: la conoscenza si forma come si è formata nel tempo.
            when = f"now() + interval '{i} milliseconds'"
            stato = "originale_conservato" if ritrascrivi else "trascrizione_completata"
            payload = Jsonb({"glossario": glossario} if ritrascrivi and glossario else {})   # niente "modo": completa
            if job is None:
                job_id = conn.execute(
                    f"""INSERT INTO ingestion_jobs (owner_id, kind, idempotency_key, entry_id, recording_id, status,
                                                    payload, next_attempt_at)
                        VALUES (%s, 'rianalisi', %s, %s, %s, %s, %s, {when}) RETURNING id""",
                    (uid, f"rianalisi:{e['id']}:{uuid4()}", e["id"], e["recording_id"], stato,
                     payload)).fetchone()["id"]
                prima = "ricevuto"
            else:
                job_id, prima = job["id"], job["stato"]
                conn.execute(
                    f"""UPDATE ingestion_jobs SET status = %s, failed_stage = NULL, payload = %s,
                               recording_id = coalesce(recording_id, %s), entry_id = coalesce(entry_id, %s),
                               attempts = 0, error_code = NULL, last_error = NULL, completed_at = NULL,
                               locked_at = NULL, locked_by = NULL, next_attempt_at = {when}
                         WHERE id = %s""", (stato, payload, e["recording_id"], e["id"], job_id))
            conn.execute("UPDATE entries SET processing_status = %s WHERE id = %s", (stato, e["id"]))
            conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (uid, job_id, prima, stato, ("nuova trascrizione" if ritrascrivi else "rianalisi") +
                          " da riga di comando" + (" con le voci" if a.voci else "")))
            ritrascritte += ritrascrivi
            in_coda += 1
    return in_coda, saltate, ritrascritte


def cmd_rianalizza(a: argparse.Namespace) -> None:
    """Rimette le note in coda dall'analisi: la trascrizione resta quella salvata.

    --voci     riconosce di nuovo chi parla (dall'audio, senza ritrascrivere)
    --ritrascrivi  rifà anche la trascrizione dall'audio originale (con il glossario dei
               nomi dell'archivio, preso prima di cancellare qualcosa); chi parla viene
               riconosciuto di nuovo. Le trascrizioni corrette a mano restano, salvo
               --anche-corrette
    --da-capo  con --tutte: cancella l'albero dei temi con le loro schede e gli appunti, e
               disfa le chiusure automatiche; tutto si riforma nota dopo nota dalla più
               vecchia. I tag messi a mano, i percorsi confermati e le indicazioni restano
    Le cose da fare spuntate o toccate a mano restano; le altre si rifanno.
    """
    if not a.tutte and not a.nota:
        sys.exit("Indica --tutte oppure --nota ID (bastano i primi caratteri dell'id, come nell'indirizzo della nota)")
    if a.da_capo and not a.tutte:
        sys.exit("--da-capo si usa solo con --tutte")
    if a.anche_corrette and not a.ritrascrivi:
        sys.exit("--anche-corrette si usa solo con --ritrascrivi")
    uid = _utente(a.utente)
    in_coda = saltate = ritrascritte = 0
    diari = _diari(uid, a.diario)
    per_diario = _note_per_diario(uid, diari, a.nota) if a.nota else {d["id"]: [] for d in diari}
    for d in diari:
        if d["id"] not in per_diario:
            continue
        _intestazione(diari, d)
        n = _rianalizza_diario(a, uid, d["id"], per_diario[d["id"]])
        in_coda, saltate, ritrascritte = in_coda + n[0], saltate + n[1], ritrascritte + n[2]
    print(f"In coda: {in_coda} {'nota' if in_coda == 1 else 'note'}" + (f", saltate: {saltate}" if saltate else ""))
    if ritrascritte:
        print(f"Da ritrascrivere: {ritrascritte}. Il worker le elabora una alla volta, dalla più vecchia: "
              "lo stato si vede nell'archivio.")
    elif in_coda:
        print("Il worker le rianalizza una alla volta, senza ritrascrivere: lo stato si vede nell'archivio.")


def _ricolloca_diario(a: argparse.Namespace, uid, diario, prefissi: list[str]) -> tuple[int, int]:
    from .coda import rimetti_in_coda
    from .conoscenza import pulisci_orfani

    with user_tx(uid, diario) as conn:
        q = "SELECT e.id, e.titolo FROM entries e WHERE e.deleted_at IS NULL AND e.trascrizione_grezza IS NOT NULL"
        if a.tutte:
            entries = conn.execute(q + " ORDER BY coalesce(e.data_ora, e.created_at)").fetchall()
        else:
            entries = []
            for pref in prefissi:
                entries += conn.execute(q + " AND e.id::text LIKE %s", (pref.lower() + "%",)).fetchall()
        if a.da_capo:
            from .evoluzione import azzera

            azzera(conn, tieni_appunti=True)
            pulisci_orfani(conn)
            print("Albero dei temi cancellato: si riforma nota dopo nota, dalla più vecchia (gli appunti restano).")
        in_coda = saltate = 0
        for i, e in enumerate(entries):
            ok = rimetti_in_coda(conn, uid, e["id"], "colloca", "nuova collocazione da riga di comando", ritardo_ms=i)
            if ok:
                in_coda += 1
            else:
                saltate += 1
                print(f"  saltata (in elaborazione): {e['titolo'] or e['id']}")
    return in_coda, saltate


def cmd_ricolloca(a: argparse.Namespace) -> None:
    """Rifà solo la collocazione degli argomenti nell'albero dei temi: gli appunti restano.

    --da-capo  con --tutte: l'albero si ricostruisce da zero, nota dopo nota dalla più
               vecchia, con le schede dei temi e le chiusure automatiche. I percorsi
               confermati dalla persona e le sue indicazioni si riapplicano.
    """
    if not a.tutte and not a.nota:
        sys.exit("Indica --tutte oppure --nota ID")
    if a.da_capo and not a.tutte:
        sys.exit("--da-capo si usa solo con --tutte")
    uid = _utente(a.utente)
    in_coda = saltate = 0
    diari = _diari(uid, a.diario)
    per_diario = _note_per_diario(uid, diari, a.nota) if a.nota else {d["id"]: [] for d in diari}
    for d in diari:
        if d["id"] not in per_diario:
            continue
        _intestazione(diari, d)
        n = _ricolloca_diario(a, uid, d["id"], per_diario[d["id"]])
        in_coda, saltate = in_coda + n[0], saltate + n[1]
    print(f"In coda: {in_coda} {'nota' if in_coda == 1 else 'note'}" + (f", saltate: {saltate}" if saltate else ""))
    if in_coda:
        print("Il worker le colloca una alla volta, dalla più vecchia, e ogni tanto riordina l'albero.")


def cmd_riordina(a: argparse.Namespace) -> None:
    """Riordina subito l'albero dei temi (di solito lo fa il worker ogni tante note)."""
    from .pipeline import build_pipeline
    from .worker import Worker

    from .config import get_settings

    uid = _utente(a.utente)
    s = get_settings()
    w = Worker(build_pipeline(s), s, worker_id="riga-di-comando")
    diari = _diari(uid, a.diario)
    for d in diari:
        _intestazione(diari, d)
        fatto = w.riordina_se_serve(uid, forza=True, diario=d["id"])
        print("\n".join(f"  {x}" for x in fatto) if fatto else "L'albero va bene così: nessuna modifica.")


def _scrivi_env(chiave: str, valore: str) -> None:
    from .config import PROJECT_ROOT

    env = PROJECT_ROOT / ".env"
    righe = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    nuove, fatto = [], False
    for r in righe:
        if r.startswith(f"{chiave}="):
            if not fatto:
                nuove.append(f"{chiave}={valore}")
                fatto = True
            continue
        nuove.append(r)
    if not fatto:
        nuove.append(f"{chiave}={valore}")
    env.write_text("\n".join(nuove) + "\n", encoding="utf-8")


def candidati_analisi(vram_mb: int | None) -> list[str]:
    """Modelli di analisi dal migliore, per quanta memoria video c'è (ottobre 2026).

    Prima i modelli Mistral a pesi aperti (Apache 2.0): Ministral 3 in tre taglie, poi
    Mistral Small 3.2 dove ci sta intero. Dopo, come riserva, Gemma 4 e Qwen 3.5.
    Contano i modelli che restano tutti sulla scheda video insieme al contesto: con 16 GB
    Mistral Small (15 GB) non ci sta, Ministral 14B (9 GB) sì.
    """
    v = vram_mb or 0
    if v >= 22000:
        return ["mistral-small3.2:24b", "ministral-3:14b", "gemma4:26b"]
    if v >= 14000:
        return ["ministral-3:14b", "gemma4:12b", "qwen3.5:9b"]
    if v >= 7000:
        return ["ministral-3:8b", "qwen3.5:9b", "gemma4:e4b"]
    return ["ministral-3:3b", "qwen3.5:4b", "gemma4:e2b"]


def cmd_scegli_modello(a: argparse.Namespace) -> None:
    """Prova i candidati dal migliore e tiene il primo che sta tutto nella scheda video.

    Un modello che non ci sta viene diviso tra scheda video e RAM: funziona, ma è lento
    e occupa la memoria del PC. La prova usa il contesto vero dell'analisi.
    """
    from .config import get_settings
    from .pipeline.errors import PipelineError
    from .pipeline.ollama import OllamaClient
    from .pipeline.transcribe import gpu_memory_mb

    s = get_settings()
    client = OllamaClient(s.ollama_url, timeout=600)
    vram = gpu_memory_mb()
    cands = [a.modello] if a.modello else candidati_analisi(vram)
    print(f"Memoria video: {vram or 0} MB · contesto dell'analisi: {s.analysis_max_context} token")
    for m in client.loaded():             # libera la scheda per una prova onesta
        client.unload(m.get("name", ""))
    scelto, migliore = None, (0.0, None)
    for m in cands:
        have = client.installed_models()
        if not any(x == m or x == f"{m}:latest" for x in have):
            print(f"  scarico {m}…", flush=True)
            try:
                client.pull(m)
            except Exception as e:
                print(f"  ✗ {m}: download non riuscito ({type(e).__name__})")
                continue
        try:
            client.load(m, s.analysis_max_context)
        except PipelineError as e:
            print(f"  ✗ {m}: non si carica ({e})")
            continue
        info = next((x for x in client.loaded() if x.get("name") in (m, f"{m}:latest")), None)
        client.unload(m)
        if not info or not info.get("size"):
            print(f"  ? {m}: Ollama non dice quanta memoria usa")
            continue
        size, vr = info["size"], info.get("size_vram", 0)
        quota = vr / size
        gb = size / 1024**3
        if quota >= 0.99:
            print(f"  ✓ {m}: {gb:.1f} GB, tutto sulla scheda video")
            scelto = m
            break
        print(f"  – {m}: {gb:.1f} GB, {(size - vr) / 1024**3:.1f} GB finirebbero nella RAM")
        if quota > migliore[0]:
            migliore = (quota, m)
    scelto = scelto or migliore[1]
    if not scelto:
        sys.exit("Nessun modello utilizzabile: Ollama è avviato? (scripts/prepara-ai.sh)")
    _scrivi_env("OLLAMA_MODEL", scelto)
    print(f"Modello di analisi: {scelto} (scritto in .env)")


def cmd_riprova_errori(a: argparse.Namespace) -> None:
    uid = _user_id(a.username)
    n = 0
    for d in _diari(uid):
        with user_tx(uid, d["id"]) as conn:
            rows = conn.execute(
                "UPDATE ingestion_jobs SET status = 'errore', attempts = 0, next_attempt_at = now(), locked_at = NULL, "
                "locked_by = NULL WHERE status IN ('errore', 'errore_definitivo') RETURNING id").fetchall()
            for r in rows:
                conn.execute("INSERT INTO job_events (owner_id, job_id, from_status, to_status, message) "
                             "VALUES (%s, %s, 'errore', 'errore', 'rimesso in coda da riga di comando')",
                             (uid, r["id"]))
        n += len(rows)
    print(f"Rimessi in coda: {n}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="diario")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    u = sub.add_parser("create-user")
    u.add_argument("username")
    u.add_argument("--password", help="solo per automazione; altrimenti viene chiesta")
    u.add_argument("--reset", action="store_true")
    u.add_argument("--admin", action="store_true", help="amministratore: gestisce utenti e inviti")
    u.set_defaults(fn=cmd_create_user)
    sub.add_parser("utenti", help="persone, ruoli, ultimo accesso").set_defaults(fn=cmd_utenti)
    i = sub.add_parser("invito", help="codice monouso con cui una persona si crea l'account")
    i.add_argument("--nota", help="per chi è, es. \"per Marta\"")
    i.add_argument("--giorni", type=int, default=7, help="quanti giorni vale (predefinito 7)")
    i.set_defaults(fn=cmd_invito)
    for nome, attivo in (("disattiva", False), ("riattiva", True)):
        x = sub.add_parser(nome)
        x.add_argument("username")
        x.set_defaults(fn=cmd_attivo, attivo=attivo)
    x = sub.add_parser("password-provvisoria", help="nuova password da cambiare al primo accesso")
    x.add_argument("username")
    x.set_defaults(fn=cmd_password_provvisoria)
    x = sub.add_parser("rendi-admin")
    x.add_argument("username")
    x.add_argument("--togli", action="store_true")
    x.set_defaults(fn=cmd_rendi_admin)
    x = sub.add_parser("diari", help="i diari di una persona")
    x.add_argument("username")
    x.set_defaults(fn=cmd_diari)
    x = sub.add_parser("elimina-utente", help="account e tutti i suoi diari, audio compresi")
    x.add_argument("username")
    x.set_defaults(fn=cmd_elimina_utente)
    t = sub.add_parser("create-token")
    t.add_argument("username")
    t.add_argument("name")
    t.set_defaults(fn=cmd_create_token)
    v = sub.add_parser("verify-storage")
    v.add_argument("username")
    v.set_defaults(fn=cmd_verify_storage)
    c = sub.add_parser("cleanup-uploads")
    c.add_argument("username")
    c.set_defaults(fn=cmd_cleanup_uploads)
    sub.add_parser("prepara-modelli").set_defaults(fn=cmd_prepara_modelli)
    d = sub.add_parser("diagnosi")
    d.add_argument("--veloce", action="store_true", help="senza caricare i modelli")
    d.set_defaults(fn=cmd_diagnosi)
    e = sub.add_parser("riprova-errori")
    e.add_argument("username")
    e.set_defaults(fn=cmd_riprova_errori)
    r = sub.add_parser("rianalizza", help="rifà l'analisi dalle trascrizioni salvate")
    r.add_argument("--tutte", action="store_true", help="tutte le note, dalla più vecchia")
    r.add_argument("--nota", nargs="+", metavar="ID", help="solo queste note (bastano i primi caratteri dell'id)")
    r.add_argument("--da-capo", action="store_true", help="con --tutte: ricrea l'albero dei temi da zero")
    r.add_argument("--voci", action="store_true", help="riconosce di nuovo chi parla, senza ritrascrivere")
    r.add_argument("--persone", type=int, help="con --voci: quante persone parlano, se lo sai")
    r.add_argument("--ritrascrivi", action="store_true", help="rifà anche la trascrizione dall'audio originale")
    r.add_argument("--anche-corrette", action="store_true",
                   help="con --ritrascrivi: anche le trascrizioni corrette a mano (la correzione si perde)")
    r.add_argument("--utente")
    r.add_argument("--diario", help="solo questo diario (nome); altrimenti tutti")
    r.set_defaults(fn=cmd_rianalizza)

    r = sub.add_parser("ricolloca", help="rifà solo la collocazione nell'albero dei temi (gli appunti restano)")
    r.add_argument("--tutte", action="store_true")
    r.add_argument("--nota", nargs="+")
    r.add_argument("--da-capo", action="store_true", help="con --tutte: ricostruisce l'albero da zero")
    r.add_argument("--utente")
    r.add_argument("--diario", help="solo questo diario (nome); altrimenti tutti")
    r.set_defaults(fn=cmd_ricolloca)

    r = sub.add_parser("riordina", help="riordina subito l'albero dei temi")
    r.add_argument("--utente")
    r.add_argument("--diario", help="solo questo diario (nome); altrimenti tutti")
    r.set_defaults(fn=cmd_riordina)
    m = sub.add_parser("scegli-modello", help="sceglie il modello di analisi che sta nella scheda video")
    m.add_argument("--modello", help="prova solo questo")
    m.set_defaults(fn=cmd_scegli_modello)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    finally:
        if argv is None:  # da riga di comando: chiude le connessioni prima di uscire
            close_pool()


if __name__ == "__main__":
    main()
