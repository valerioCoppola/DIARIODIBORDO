"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { HoldButton } from "@/components/HoldButton";
import { Shell, formatBytes, formatDuration, formatWhen, useSession } from "@/components/Shell";
import {
  deleteRecording, listRecordings, newId, putRecording, appendPiece, recoverInterrupted,
  requestPersistence, updateRecording, type LocalRecording,
} from "@/lib/localstore";
import { guardiaChiusura } from "@/lib/diario";
import { Recorder, recordingSupported, type RecorderState } from "@/lib/recorder";
import { uploadRecording } from "@/lib/uploader";

const STATO_LOCALE: Record<LocalRecording["status"], string> = {
  in_registrazione: "In registrazione",
  da_inviare: "Sul telefono, da inviare",
  in_invio: "Invio al PC",
  conservato: "Conservato sul PC",
  errore: "Invio non riuscito",
};

export default function RegistraPage() {
  const { me } = useSession();
  const [state, setState] = useState<RecorderState>("fermo");
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [, setSaved] = useState(0);
  const [items, setItems] = useState<LocalRecording[]>([]);
  const [notice, setNotice] = useState<{ text: string; grave?: boolean } | null>(null);
  const [support, setSupport] = useState<{ ok: boolean; reason?: string }>({ ok: true });
  const recorder = useRef<Recorder | null>(null);
  const diarioId = me?.diario?.id;

  // Su questo telefono: solo i vocali di questo diario. Quelli fatti prima che ci fossero
  // più diari diventano di questo.
  const refresh = useCallback(async () => {
    const tutti = await listRecordings();
    for (const r of tutti) if (!r.diarioId && diarioId) Object.assign(r, await updateRecording(r.id, { diarioId }));
    setItems(tutti.filter((r) => r.diarioId === diarioId));
  }, [diarioId]);
  const onChange = useCallback((r: LocalRecording) => {
    setItems((prev) => prev.map((p) => (p.id === r.id ? r : p)));
  }, []);

  const send = useCallback(
    (id: string) => uploadRecording(id, onChange).catch(() => {}),
    [onChange],
  );

  const sendPending = useCallback(async () => {
    for (const r of await listRecordings()) {
      if (r.status === "da_inviare" || r.status === "errore") await send(r.id);
    }
  }, [send]);

  useEffect(() => {
    if (!me) return;
    setSupport(recordingSupported());
    void requestPersistence();
    recorder.current = new Recorder({
      onState: setState,
      onLevel: setLevel,
      onSaved: setSaved,
      onStop: async (id, reason) => {
        if (reason === "eliminata") {
          setElapsed(0);
          setSaved(0);
          setNotice({ text: "Registrazione eliminata." });
          await refresh();
          return;
        }
        if (reason === "microfono_perso") {
          setNotice({ text: "Il microfono si è interrotto (ad esempio per una chiamata). Quanto registrato è salvato." });
        } else if (reason === "errore") {
          setNotice({ text: "La registrazione si è interrotta per un errore del browser. Quanto registrato è salvato.", grave: true });
        }
        await refresh();
        void send(id);
      },
    });
    (async () => {
      const n = await recoverInterrupted(null);
      if (n > 0) setNotice({ text: n === 1
        ? "Recuperata una registrazione interrotta: viene inviata al PC."
        : `Recuperate ${n} registrazioni interrotte: vengono inviate al PC.` });
      await refresh();
      void sendPending();
    })();
    const online = () => void sendPending();
    window.addEventListener("online", online);
    return () => window.removeEventListener("online", online);
  }, [me, refresh, send, sendPending]);

  // Cronometro
  useEffect(() => {
    if (state === "fermo") return;
    const t = setInterval(() => setElapsed(recorder.current?.elapsedMs() ?? 0), 250);
    return () => clearInterval(t);
  }, [state]);

  // Il diario non si chiude a metà di una registrazione.
  useEffect(() => {
    guardiaChiusura(() => (recorder.current && recorder.current.state !== "fermo"
      ? "Stai registrando: termina prima di chiudere il diario" : null));
    return () => guardiaChiusura(null);
  }, []);

  // Avviso se si prova a chiudere la pagina durante la registrazione
  useEffect(() => {
    if (state === "fermo") return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [state]);

  async function toggle() {
    const r = recorder.current;
    if (!r) return;
    if (state === "fermo") {
      setNotice(null);
      setElapsed(0);
      setSaved(0);
      try {
        await r.start(diarioId);
        await refresh();
      } catch (e) {
        const denied = e instanceof DOMException && e.name === "NotAllowedError";
        setNotice({ grave: true, text: denied
          ? "Accesso al microfono negato. Consentilo nelle impostazioni del sito e riprova."
          : `Impossibile avviare la registrazione: ${e instanceof Error ? e.message : e}` });
      }
    } else {
      r.stop();
    }
  }

  async function onFile(ev: React.ChangeEvent<HTMLInputElement>) {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    const id = newId();
    await putRecording({
      id, kind: "file", diarioId, createdAt: new Date().toISOString(), startedAt: null, durationMs: null,
      mimeType: file.type || "audio/mpeg", filename: file.name, bytes: 0, pieces: 0, status: "in_registrazione",
    });
    await appendPiece(id, 0, file); // copia locale finché il PC non conferma
    await updateRecording(id, { status: "da_inviare" });
    await refresh();
    void send(id);
  }

  async function remove(id: string) {
    await deleteRecording(id);
    await refresh();
  }

  if (!me) return <Shell active="registra" />;

  const statoRiga =
    state === "registra" ? "In registrazione"
    : state === "pausa" ? "In pausa"
    : support.ok ? "" : support.reason;

  return (
    <Shell active="registra">
      {notice && <p className={`avviso${notice.grave ? " grave" : ""}`} role="status">{notice.text}</p>}

      <section className="ponte" data-stato={state} aria-label="Registratore">
        <p className="cronometro" aria-live="off">{formatDuration(elapsed)}</p>
        <div className="livello" aria-hidden="true">
          <span style={{ transform: `scaleX(${level})` }} />
        </div>
        <p className="stato-riga" role="status">{statoRiga}</p>
        <div className="comandi">
          <button className="secondario" type="button" disabled={state === "fermo"}
            onClick={() => (state === "pausa" ? recorder.current?.resume() : recorder.current?.pause())}>
            {state === "pausa" ? "Riprendi" : "Pausa"}
          </button>
          <button className="rec" type="button" onClick={toggle} disabled={!support.ok}
            data-attivo={state !== "fermo"}
            aria-label={state === "fermo" ? "Inizia a registrare" : "Termina e salva"} />
          {state === "pausa" ? (
            <HoldButton label="Elimina" hint="tieni premuto 2 s" onConfirm={() => recorder.current?.discard()} />
          ) : (
            <span style={{ minWidth: "5.5rem" }} aria-hidden="true" />
          )}
        </div>
      </section>

      <div className="carica">
        <input id="file" type="file" accept="audio/*,.m4a,.mp3,.wav,.ogg,.opus,.webm,.flac" onChange={onFile} />
        <label htmlFor="file">Oppure carica un file audio</label>
      </div>

      <section className="registro" aria-labelledby="sul-telefono">
        <h2 id="sul-telefono">Su questo telefono</h2>
        {items.length === 0 ? (
          <p className="vuoto">Nessuna registrazione.</p>
        ) : (
          <ul className="voci">
            {items.map((r) => (
              <li className="voce" key={r.id}>
                <span className="quando">{r.kind === "file" ? r.filename : formatWhen(r.startedAt ?? r.createdAt)}</span>
                <span className="dettagli">
                  {[r.durationMs ? formatDuration(r.durationMs) : null, r.bytes ? formatBytes(r.bytes) : null,
                    r.recovered ? "recuperata" : null, r.duplicate ? "già presente sul PC" : null]
                    .filter(Boolean).join(", ")}
                </span>
                <span className="azioni">
                  <span className="stato" data-s={r.status}>{STATO_LOCALE[r.status]}</span>
                </span>
                {r.status === "in_invio" && (
                  <div className="barra" role="progressbar" aria-valuenow={Math.round((r.progress ?? 0) * 100)}
                    aria-valuemin={0} aria-valuemax={100} aria-label="Invio">
                    <span style={{ width: `${Math.round((r.progress ?? 0) * 100)}%` }} />
                  </div>
                )}
                {r.status === "errore" && (
                  <span className="errore">
                    {r.error} <button className="piccolo" type="button" onClick={() => send(r.id)}>Riprova l’invio</button>
                  </span>
                )}
                {r.status === "conservato" && (
                  <span className="errore" style={{ color: "var(--secondario)" }}>
                    <button className="piccolo" type="button" onClick={() => remove(r.id)}>Togli dall’elenco</button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </Shell>
  );
}
