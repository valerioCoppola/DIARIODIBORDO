"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Appunti, type BloccoAppunti, type Voce as VoceAppunti } from "@/components/Appunti";
import { Chiarimenti, type Chiarimento } from "@/components/Chiarimenti";
import { HoldButton } from "@/components/HoldButton";
import { Lettore } from "@/components/Lettore";
import { Shell, formatWhen, useSession } from "@/components/Shell";
import { ApiError, api } from "@/lib/api";
import { durata, type Voce } from "@/lib/archivio";
import { coloreTinta } from "@/lib/grafo";
import { CONCLUSI, FASE, spiegaErrore } from "@/lib/fasi";
import { consegna, prepara } from "@/lib/salva";

type Segmento = { inizio: number; fine: number; char_start: number; char_end: number; voce?: number };
type Azione = { id: string; descrizione: string; responsabile: string | null; scadenza: string | null;
  stato: "aperta" | "completata" | "annullata"; fonte: string | null; secondi: number | null; ribadita: number;
  chiusa_da: { id: string; titolo: string | null } | null };
type AltraAzione = { id: string; descrizione: string; stato: "aperta" | "completata" | "annullata";
  effetto: "chiusa" | "ribadita"; nota: { id: string; titolo: string | null } };
type Entry = {
  id: string; titolo: string | null; categoria: string; data_ora: string | null; creato_il: string;
  executive_summary: string[] | null; stato: string; recording_id: string | null; tag: string[];
  concetti: { id: string; nome: string; tinta: number; percorso: string[]; provvisorio: boolean }[];
  chiarimenti: Chiarimento[];
  voci: { nomi: Record<string, string>; persone: number; stato: string | null };
  trascrizione_grezza: string | null; trascrizione_sha: string; segmenti: Segmento[]; durata_secondi: number | null;
  audio_disponibile: boolean;
  action_items: Azione[];
  appunti: BloccoAppunti[];
  altre_azioni: AltraAzione[];
  analisi: { versione: number; modello: string | null; avvisi: string[]; il: string } | null;
  trascrizione: { modello: string; dispositivo: string; lingua: string | null } | null;
  job: { id: string; stato: string; codice_errore: string | null; errore: string | null } | null;
};

type Paragrafo = { key: number; voce?: number; parti: { seg: number | null; testo: string; inizio?: number }[] };

/** Il testo originale diviso in paragrafi, con ogni segmento legato al suo istante nell'audio. */
function paragrafi(testo: string, segs: Segmento[]): Paragrafo[] {
  const out: Paragrafo[] = [{ key: 0, parti: [] }];
  let pos = 0;
  const push = (t: string, seg: number | null, inizio?: number) => {
    const blocks = t.split(/\n\s*\n/);
    blocks.forEach((b, i) => {
      if (i > 0) out.push({ key: out.length, parti: [] });
      if (b) out[out.length - 1].parti.push({ seg, testo: b, inizio });
    });
  };
  segs.forEach((s, i) => {
    if (s.char_start > pos) push(testo.slice(pos, s.char_start), null);
    push(testo.slice(s.char_start, s.char_end), i, s.inizio);
    pos = s.char_end;
  });
  if (pos < testo.length) push(testo.slice(pos), null);
  return out.filter((p) => p.parti.length > 0);
}

/** Con più voci: un paragrafo per intervento, introdotto da chi parla. */
function interventi(testo: string, segs: Segmento[]): Paragrafo[] {
  const out: Paragrafo[] = [];
  segs.forEach((s, i) => {
    const t = testo.slice(s.char_start, s.char_end).replace(/\s+/g, " ").trim();
    if (!t) return;
    const ultimo = out[out.length - 1];
    if (ultimo && ultimo.voce === s.voce) {
      ultimo.parti.push({ seg: null, testo: " " }, { seg: i, testo: t, inizio: s.inizio });
    } else {
      out.push({ key: out.length, voce: s.voce, parti: [{ seg: i, testo: t, inizio: s.inizio }] });
    }
  });
  return out;
}

const nomeVoce = (v: number | undefined, nomi: Record<string, string>) =>
  v ? (nomi[String(v)]?.trim() || `Voce ${v}`) : "Voce ?";

/** Link a un file che non porta fuori dall'app: scarica e apre la condivisione del telefono. */
function Scarica({ url, nome, children }: { url: string; nome: string; children: string }) {
  const [stato, setStato] = useState<"" | "prepara" | "pronto">("");
  const [errore, setErrore] = useState<string | null>(null);
  const file = useRef<File | null>(null);

  async function tocca(ev: React.MouseEvent) {
    ev.preventDefault();
    if (stato === "prepara") return;
    setErrore(null);
    try {
      if (!file.current) {
        setStato("prepara");
        file.current = await prepara(url, nome);
      }
      const esito = await consegna(file.current);
      if (esito === "serve-tocco") {
        setStato("pronto");
      } else {
        setStato("");
        file.current = null;
      }
    } catch (e) {
      setStato("");
      file.current = null;
      setErrore(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <>
      <a href={url} onClick={tocca} aria-busy={stato === "prepara"}>
        {stato === "prepara" ? "Preparazione…" : stato === "pronto" ? `${children}: tocca per salvare` : children}
      </a>
      {errore && <span className="avviso grave" role="alert">{errore}</span>}
    </>
  );
}

export default function NotaPage() {
  const { me } = useSession();
  const [id, setId] = useState<string | null>(null);
  const [recId, setRecId] = useState<string | null>(null);
  const [rec, setRec] = useState<Voce | null>(null);
  const [eliminando, setEliminando] = useState(false);
  const pendingSeek = useRef<number | null>(null);
  const [e, setE] = useState<Entry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<number | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const [bozza, setBozza] = useState<string | null>(null);   // testo in correzione
  const [salvando, setSalvando] = useState(false);
  const [erroreBozza, setErroreBozza] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const inCorrezione = bozza !== null;
  useEffect(() => {
    if (inCorrezione) area.current?.scrollIntoView({ block: "center" });
  }, [inCorrezione]);

  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    setId(p.get("id"));
    setRecId(p.get("r"));
    const t = Number(p.get("t"));
    if (Number.isFinite(t) && t > 0) {
      pendingSeek.current = t;
    }
  }, []);

  const load = useCallback(async () => {
    if (!id && !recId) return;
    try {
      if (id) {
        setE(await api<Entry>(`/api/v1/entries/${id}`));
      } else {
        // Vocale non ancora trascritto: appena c'è la nota, si passa alla nota.
        const r = await api<Voce>(`/api/v1/recordings/${recId}`);
        if (r.nota) {
          window.history.replaceState(null, "", `/nota/?id=${r.nota.id}`);
          setId(r.nota.id);
          return;
        }
        setRec(r);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404 ? "Nota non trovata." : String(err instanceof Error ? err.message : err));
    }
  }, [id, recId]);

  useEffect(() => {
    if (me && (id || recId)) void load();
  }, [me, id, recId, load]);

  const statoCorrente = e?.stato ?? rec?.job?.stato ?? null;
  const inCorso = statoCorrente !== null && !CONCLUSI.has(statoCorrente);
  useEffect(() => {
    if (!inCorso) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [inCorso, load]);

  const conVoci = (e?.voci?.persone ?? 0) > 1;
  const parti = useMemo(() => (!e?.trascrizione_grezza ? []
    : conVoci ? interventi(e.trascrizione_grezza, e.segmenti) : paragrafi(e.trascrizione_grezza, e.segmenti)), [e, conVoci]);
  const [nomi, setNomi] = useState<Record<string, string> | null>(null);   // nomi delle voci in modifica
  const [salvaNomi, setSalvaNomi] = useState(false);

  async function salvaVoci() {
    if (!e || !nomi) return;
    setSalvaNomi(true);
    try {
      await api(`/api/v1/entries/${e.id}/voci`, { method: "PUT", json: { nomi } });
      setNomi(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSalvaNomi(false);
    }
  }

  function seek(t: number) {
    const a = audio.current;
    if (!a) return;
    // Prima che l'audio sia pronto la posizione non si può impostare: la si applica appena lo è.
    if (a.readyState < 1) {
      pendingSeek.current = t;
      a.load();
    } else {
      a.currentTime = t;
    }
    void a.play().catch(() => {});
  }

  function onReady() {
    const a = audio.current;
    if (a && pendingSeek.current !== null) {
      a.currentTime = pendingSeek.current;
      pendingSeek.current = null;
    }
  }

  async function elimina() {
    const url = e ? `/api/v1/entries/${e.id}` : `/api/v1/recordings/${rec?.id}`;
    setEliminando(true);
    try {
      await api(url, { method: "DELETE" });
      window.location.replace("/archivio/");
    } catch (err) {
      setEliminando(false);
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const audioId = e?.recording_id ?? rec?.id ?? null;
  const audioOk = e ? e.audio_disponibile : rec?.audio_disponibile ?? false;

  function onTime() {
    const a = audio.current;
    if (!a || !e) return;
    const t = a.currentTime;
    const i = e.segmenti.findIndex((s) => t >= s.inizio && t < s.fine + 0.3);
    setCurrent(i >= 0 ? i : null);
  }

  async function spunta(id: string, prima: string) {
    const stato = prima === "completata" ? "aperta" : "completata";
    const aggiorna = (v: VoceAppunti): VoceAppunti => ({
      ...v, azione: v.azione && v.azione.id === id ? { ...v.azione, stato, chiusa_da: null } : v.azione,
      dettagli: v.dettagli.map(aggiorna),
    });
    setE((prev) => prev && { ...prev, appunti: prev.appunti.map((b) => ({ ...b, punti: b.punti.map(aggiorna) })) });
    try {
      await api(`/api/v1/action-items/${id}`, { method: "PATCH", json: { stato } });
    } catch {
      await load();
    }
  }

  async function retry() {
    const jobId = e?.job?.id ?? rec?.job?.id;
    if (!jobId) return;
    await api(`/api/v1/jobs/${jobId}/riprova`, { method: "POST" }).catch(() => {});
    await load();
  }

  const modificata = bozza !== null && e !== null && bozza !== e.trascrizione_grezza;
  useEffect(() => {
    if (!modificata) return;
    const h = (ev: BeforeUnloadEvent) => ev.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [modificata]);

  async function salvaBozza() {
    if (!e || bozza === null) return;
    if (!modificata) { setBozza(null); return; }
    setSalvando(true);
    setErroreBozza(null);
    try {
      await api(`/api/v1/entries/${e.id}/trascrizione`, {
        method: "PUT", json: { testo: bozza, base: e.trascrizione_sha },
      });
      setBozza(null);
      await load();
    } catch (err) {
      setErroreBozza(err instanceof Error ? err.message : String(err));
    } finally {
      setSalvando(false);
    }
  }

  if (!me) return <Shell active="archivio" />;

  const meta = e
    ? [e.data_ora ? formatWhen(e.data_ora) : `${formatWhen(e.creato_il)} (data di arrivo)`,
       e.titolo ? e.categoria : null, durata(e.durata_secondi)]
    : rec
      ? [formatWhen(rec.registrato_il ?? rec.creato_il), durata(rec.durata_secondi)]
      : [];
  const job = e?.job ?? (rec?.job ? { id: rec.job.id, stato: rec.job.stato, codice_errore: rec.job.errore,
    errore: rec.job.messaggio_errore } : null);

  return (
    <Shell active="archivio">
      <p className="indietro"><a href="/archivio/">← Archivio</a></p>
      {error && <p className="avviso grave" role="alert">{error}</p>}
      {!e && !rec && !error && <p className="vuoto">Caricamento…</p>}
      {(e || rec) && (
        <article className="scheda">
          <header>
            <h2 className="scheda-titolo">{e?.titolo ?? "Vocale in elaborazione"}</h2>
            <p className="scheda-meta">{meta.filter(Boolean).join(" · ")}</p>
            {e && e.concetti.length > 0 && (
              <ul className="tag" aria-label="Argomenti">
                {e.concetti.map((c) => (
                  <li key={c.id}>
                    <a href={`/mappa/?c=${c.id}`} title={[...c.percorso, c.nome].join(" › ")}
                      data-provvisorio={c.provvisorio || undefined}
                      style={{ "--c": coloreTinta(c.tinta) } as React.CSSProperties}>
                      {c.nome}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </header>

          {inCorso && statoCorrente && (
            <p className="avviso" role="status">
              <span className="stato" data-s={statoCorrente}>{FASE[statoCorrente] ?? statoCorrente}</span>
              {statoCorrente === "errore" && job?.codice_errore && <> — {spiegaErrore(job.codice_errore) ?? job.errore}</>}
            </p>
          )}
          {statoCorrente === "errore_definitivo" && (
            <p className="avviso grave" role="alert">
              {spiegaErrore(job?.codice_errore) ?? job?.errore ?? "L’elaborazione si è fermata."}{" "}
              <button className="piccolo" type="button" onClick={retry}>Riprova</button>
            </p>
          )}

          {audioId && audioOk && (
            <>
              <audio ref={audio} preload="metadata" src={`/api/v1/recordings/${audioId}/audio`}
                onTimeUpdate={onTime} onLoadedMetadata={onReady} />
              <Lettore audio={audio} durata={e?.durata_secondi ?? rec?.durata_secondi} fisso={bozza !== null} />
            </>
          )}

          {e && (e.appunti.length > 0 || e.altre_azioni.length > 0) && (
            <section aria-labelledby="appunti" className="sezione-appunti" data-aggiorna={inCorso || undefined}>
              <h3 id="appunti">Appunti</h3>
              <Appunti blocchi={e.appunti} onSpunta={(a) => void spunta(a.id, a.stato)} />
              {e.chiarimenti?.length > 0 && (
                <div className="chiarire-qui">
                  <p className="nota-piccola">Non sono sicuro di dove vada:</p>
                  <Chiarimenti voci={e.chiarimenti} conNota={false} onRisposto={() => { setTimeout(() => void load(), 1500); }} />
                </div>
              )}
              {e.altre_azioni.some((a) => a.effetto === "chiusa") && (
                <ul className="chiuse-qui">
                  {e.altre_azioni.filter((a) => a.effetto === "chiusa").map((a) => (
                    <li key={a.id}><span className="segno">fatta</span><a href={`/nota/?id=${a.nota.id}`}>{a.descrizione}</a></li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {e && bozza !== null && (
            <section aria-labelledby="trascrizione" className="correggi">
              <h3 id="trascrizione">Trascrizione</h3>
              <textarea ref={area} value={bozza} onChange={(ev) => setBozza(ev.target.value)} aria-label="Trascrizione"
                spellCheck lang="it" autoFocus rows={Math.min(30, Math.max(8, Math.ceil(bozza.length / 38)))} />
              {erroreBozza && <p className="avviso grave" role="alert">{erroreBozza}</p>}
              <div className="correggi-comandi">
                <button className="primario" type="button" onClick={salvaBozza} disabled={salvando || !bozza.trim()}>
                  {salvando ? "Salvo…" : "Salva"}
                </button>
                <button className="piccolo" type="button" disabled={salvando}
                  onClick={() => { setBozza(null); setErroreBozza(null); }}>Annulla</button>
              </div>
            </section>
          )}

          {parti.length > 0 && bozza === null && (
            <section aria-labelledby="trascrizione">
              <h3 id="trascrizione">
                Trascrizione
                {e && conVoci && nomi === null && (
                  <button className="collegamento mc" type="button"
                    onClick={() => setNomi(Object.fromEntries(Array.from({ length: e.voci.persone },
                      (_, i) => [String(i + 1), e.voci.nomi[String(i + 1)] ?? ""])))}>
                    nomi
                  </button>
                )}
                {e && !inCorso && (
                  <button className="collegamento mc" type="button"
                    onClick={() => { setBozza(e.trascrizione_grezza ?? ""); setErroreBozza(null); }}>
                    correggi
                  </button>
                )}
              </h3>
              {e && nomi !== null && (
                <form className="nomi-voci" onSubmit={(ev) => { ev.preventDefault(); void salvaVoci(); }}>
                  {Object.keys(nomi).map((k) => (
                    <div className="campo" key={k}>
                      <label htmlFor={`voce-${k}`}>Voce {k}</label>
                      <input id={`voce-${k}`} value={nomi[k]} maxLength={40} autoComplete="off"
                        onChange={(ev) => setNomi({ ...nomi, [k]: ev.target.value })} />
                    </div>
                  ))}
                  <div className="correggi-comandi">
                    <button className="primario" type="submit" disabled={salvaNomi}>{salvaNomi ? "Salvo…" : "Salva"}</button>
                    <button className="piccolo" type="button" disabled={salvaNomi} onClick={() => setNomi(null)}>Annulla</button>
                  </div>
                </form>
              )}
              <div className="trascrizione" data-voci={conVoci || undefined}>
                {parti.map((p) => (
                  <p key={p.key} data-voce={p.voce}>
                    {conVoci && <span className="chi" data-voce={p.voce}>{nomeVoce(p.voce, e?.voci.nomi ?? {})}</span>}
                    {p.parti.map((x, j) => x.seg === null || !audioOk ? <span key={j}>{x.testo}</span> : (
                      <span key={j} className="seg" data-attivo={current === x.seg} role="button" tabIndex={0}
                        onClick={() => seek(x.inizio ?? 0)}
                        onKeyDown={(ev) => { if (ev.key === "Enter") seek(x.inizio ?? 0); }}>{x.testo}</span>
                    ))}
                  </p>
                ))}
              </div>
            </section>
          )}

          <footer className="provenienza">
            {e?.analisi && (
              <p>
                {[e.trascrizione?.modello, e.analisi.modello, `versione ${e.analisi.versione}`].filter(Boolean).join(" · ")}
              </p>
            )}
            {e?.analisi && e.analisi.avvisi.length > 0 && (
              <details>
                <summary>{e.analisi.avvisi.length === 1 ? "1 avviso della verifica" : `${e.analisi.avvisi.length} avvisi della verifica`}</summary>
                <ul>{e.analisi.avvisi.map((w, i) => <li key={i}>{w}</li>)}</ul>
              </details>
            )}
            <p className="scarica">
              {e?.analisi && <Scarica url={`/api/v1/entries/${e.id}/nota.json`} nome="nota.json">Nota in JSON</Scarica>}
              {audioId && audioOk && <Scarica url={`/api/v1/recordings/${audioId}/audio?originale=1`} nome="vocale">Audio originale</Scarica>}
            </p>
          </footer>

          <section className="zona-elimina" aria-labelledby="elimina-titolo">
            <h3 id="elimina-titolo">Elimina</h3>
            <p>Audio, trascrizione{e?.titolo ? " e nota" : ""}. Non si può annullare.</p>
            {eliminando ? <p className="vuoto">Eliminazione…</p> : (
              <HoldButton label="Elimina dal Diario" hint="tieni premuto 2 s" className="largo" onConfirm={elimina} />
            )}
          </section>
        </article>
      )}
    </Shell>
  );
}
