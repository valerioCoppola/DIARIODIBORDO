"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { Compiti } from "@/components/Compiti";
import { Shell, formatWhen, useSession } from "@/components/Shell";
import { api, apiStream, isoLocale } from "@/lib/api";
import { coloreTinta, dataBreve } from "@/lib/grafo";

type RamoMini = { nome: string; tinta: number };
type Risultato = {
  entry_id: string; titolo: string | null; data_ora: string | null; creato_il: string;
  executive_summary: string[] | null; frammento: string | null; secondi: number | null; per_significato: boolean;
  rami: RamoMini[];
};
type Concetto = { id: string; nome: string; note: number; tinta: number; ramo: string | null };
type Azione = { id: string; testo: string; chi: string | null; quando: string | null;
  stato: "aperta" | "completata" | "annullata"; nota: { id: string; titolo: string | null } };
type Ricerca = { modalita: "ibrida" | "testuale"; risultati: Risultato[]; concetti: Concetto[]; azioni: Azione[] };
type Ramo = { id: string; nome: string; tinta: number };
type Fonte = { n: number; entry_id: string; titolo: string; data: string; estratto: string; secondi: number | null;
  tipo: "passaggio" | "sintesi" | "azione" | "tema"; tema_id?: string | null };

const linkFonte = (f: Fonte) => (f.tipo === "tema" && f.tema_id ? `/mappa/?c=${f.tema_id}` : linkNota(f.entry_id, f.secondi));
type Risposta = { domanda: string; testo: string; fonti: Fonte[]; periodo: string | null;
  stato: "pensa" | "scrive" | "fatto" | "errore"; errore?: string };

const PERIODI = [["", "sempre"], ["settimana", "settimana"], ["mese", "mese"], ["anno", "anno"]] as const;
const RECENTI = "ddb-domande";
const linkNota = (id: string, sec: number | null) => `/nota/?id=${id}${sec ? `&t=${Math.floor(sec)}` : ""}`;

function inizio(periodo: string): string | undefined {
  if (!periodo) return undefined;
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  if (periodo === "settimana") d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  if (periodo === "mese") d.setDate(1);
  if (periodo === "anno") d.setMonth(0, 1);
  return isoLocale(d);
}

function leggiRecenti(): string[] {
  try { return JSON.parse(localStorage.getItem(RECENTI) ?? "[]"); } catch { return []; }
}
function salvaRecente(q: string): string[] {
  const l = [q, ...leggiRecenti().filter((x) => x !== q)].slice(0, 6);
  try { localStorage.setItem(RECENTI, JSON.stringify(l)); } catch { /* niente memoria locale: pazienza */ }
  return l;
}

/** Le parole trovate arrivano tra \u0001 e \u0002: diventano <mark>, senza HTML dal server. */
function Frammento({ testo }: { testo: string }) {
  return <>{testo.split(/(\u0001[^\u0002]*\u0002)/).map((p, i) =>
    p.startsWith("\u0001") ? <mark key={i}>{p.slice(1, -1)}</mark> : <Fragment key={i}>{p}</Fragment>)}</>;
}

/** Il testo della risposta con i rimandi [n] che portano alla fonte. */
function TestoRisposta({ testo, fonti }: { testo: string; fonti: Fonte[] }) {
  const per = new Map(fonti.map((f) => [f.n, f]));
  return (
    <>
      {testo.split(/(\[\d+(?:\s*[,;]\s*\d+)*\])/).map((p, i) => {
        const m = p.match(/^\[(.+)\]$/);
        if (!m) return <Fragment key={i}>{p}</Fragment>;
        return m[1].split(/[,;]/).map((x) => Number(x.trim())).filter((n) => per.has(n)).map((n) => {
          const f = per.get(n)!;
          return <a key={`${i}-${n}`} className="rif" href={linkFonte(f)} title={f.titolo}>{n}</a>;
        });
      })}
    </>
  );
}

const citate = (testo: string) =>
  new Set([...testo.matchAll(/\[(\d+(?:\s*[,;]\s*\d+)*)\]/g)].flatMap((m) => m[1].split(/[,;]/).map((x) => Number(x.trim()))));

export default function CercaPage() {
  const { me } = useSession();
  const [q, setQ] = useState("");
  const [periodo, setPeriodo] = useState("");
  const [ramo, setRamo] = useState<string | null>(null);
  const [rami, setRami] = useState<Ramo[]>([]);
  const [res, setRes] = useState<Ricerca | null>(null);
  const [risp, setRisp] = useState<Risposta | null>(null);
  const [recenti, setRecenti] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const flusso = useRef<AbortController | null>(null);

  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get("q")) setQ(p.get("q")!);
    setRecenti(leggiRecenti());
  }, []);

  useEffect(() => {
    if (me) api<Ramo[]>("/api/v1/rami").then(setRami).catch(() => {});
  }, [me]);

  // Mentre si scrive: ricerca immediata (Postgres), nessun modello.
  useEffect(() => {
    if (!me) return;
    const query = q.trim();
    const url = new URL(window.location.href);
    if (query) url.searchParams.set("q", query); else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
    if (!query) { setRes(null); return; }
    const my = ++seq.current;
    const t = setTimeout(async () => {
      const p = new URLSearchParams({ q: query });
      const dal = inizio(periodo);
      if (dal) p.set("dal", dal);
      if (ramo) p.set("ramo", ramo);
      try {
        const r = await api<Ricerca>(`/api/v1/search?${p}`);
        if (my === seq.current) { setRes(r); setError(null); }
      } catch (e) {
        if (my === seq.current) setError(e instanceof Error ? e.message : String(e));
      }
    }, 300);
    return () => clearTimeout(t);
  }, [q, me, periodo, ramo]);

  // Invio: risposta del modello, con le fonti.
  async function chiedi(domanda: string) {
    domanda = domanda.trim();
    if (domanda.length < 2) return;
    flusso.current?.abort();
    const ctl = new AbortController();
    flusso.current = ctl;
    setRecenti(salvaRecente(domanda));
    let r: Risposta = { domanda, testo: "", fonti: [], periodo: null, stato: "pensa" };
    setRisp(r);
    try {
      await apiStream("/api/v1/chiedi", { domanda, oggi: isoLocale(), ramo, dal: inizio(periodo) ?? null }, (riga) => {
        if (ctl.signal.aborted) return;
        if ("fonti" in riga) r = { ...r, fonti: riga.fonti as Fonte[], periodo: (riga.periodo as string) ?? null };
        else if ("t" in riga) r = { ...r, testo: r.testo + (riga.t as string), stato: "scrive" };
        else if ("errore" in riga) r = { ...r, stato: "errore", errore: riga.errore as string };
        else if ("fine" in riga) r = { ...r, stato: "fatto" };
        setRisp(r);
      }, ctl.signal);
      if (!ctl.signal.aborted && r.stato !== "errore") setRisp({ ...r, stato: "fatto" });
    } catch (e) {
      if (!ctl.signal.aborted) setRisp({ ...r, stato: "errore", errore: e instanceof Error ? e.message : String(e) });
    }
  }

  async function spunta(a: Azione) {
    const stato = a.stato === "completata" ? "aperta" : "completata";
    const cambia = (s: Azione["stato"]) => setRes((x) => x && {
      ...x, azioni: x.azioni.map((y) => (y.id === a.id ? { ...y, stato: s } : y)),
    });
    cambia(stato);
    try {
      await api(`/api/v1/action-items/${a.id}`, { method: "PATCH", json: { stato } });
    } catch (e) {
      cambia(a.stato);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!me) return <Shell active="cerca" />;

  const fontiMostrate = risp
    ? (risp.stato === "errore" ? risp.fonti : risp.stato === "fatto" ? risp.fonti.filter((f) => citate(risp.testo).has(f.n)) : [])
    : [];
  const vuota = res && res.risultati.length + res.concetti.length + res.azioni.length === 0;

  return (
    <Shell active="cerca">
      <form className="cerca chiedi" role="search" onSubmit={(e) => { e.preventDefault(); void chiedi(q); }}>
        <label htmlFor="q">Cerca o chiedi</label>
        <div className="chiedi-riga">
          <input id="q" type="search" value={q} onChange={(e) => setQ(e.target.value)} autoComplete="off"
            enterKeyHint="send" autoFocus />
          <button className="primario" type="submit" disabled={q.trim().length < 2 || risp?.stato === "pensa"}>Chiedi</button>
        </div>
      </form>

      <div className="filtri">
        <div className="viste" role="tablist" aria-label="Periodo">
          {PERIODI.map(([v, nome]) => (
            <button key={v} type="button" role="tab" aria-selected={periodo === v} onClick={() => setPeriodo(v)}>{nome}</button>
          ))}
        </div>
        {rami.length > 1 && (
          <ul className="vicini" aria-label="Tema">
            {rami.map((a) => (
              <li key={a.id}>
                <button type="button" className="tema-chip" aria-pressed={ramo === a.id}
                  style={{ "--c": coloreTinta(a.tinta) } as React.CSSProperties}
                  onClick={() => setRamo(ramo === a.id ? null : a.id)}>
                  <i aria-hidden="true" />{a.nome}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && <p className="avviso grave" role="alert">{error}</p>}

      {risp && (
        <section className="risposta" aria-live="polite" aria-busy={risp.stato === "pensa" || risp.stato === "scrive"}>
          <p className="risposta-domanda">{risp.domanda}{risp.periodo && <span className="indice-n"> · {risp.periodo}</span>}</p>
          {risp.stato === "pensa" && !risp.testo && <p className="risposta-testo in-attesa">…</p>}
          {risp.testo && (
            <p className="risposta-testo" data-scrive={risp.stato === "scrive" || undefined}>
              <TestoRisposta testo={risp.testo} fonti={risp.fonti} />
            </p>
          )}
          {risp.stato === "errore" && <p className="nota-piccola">{risp.errore}</p>}
          {fontiMostrate.length > 0 && (
            <ol className="fonti">
              {fontiMostrate.map((f) => (
                <li key={f.n}>
                  <a href={linkFonte(f)}>
                    <span className="fonte-n">{f.n}</span>
                    <span className="fonte-testa">{f.tipo === "tema" ? `tema · ${f.titolo}` : `${dataBreve(f.data)} · ${f.titolo}`}</span>
                    <span className="fonte-estratto">{f.estratto}</span>
                  </a>
                </li>
              ))}
            </ol>
          )}
        </section>
      )}

      {!q.trim() && !risp && recenti.length > 0 && (
        <section aria-labelledby="recenti">
          <h3 className="sezione" id="recenti">Domande recenti</h3>
          <ul className="recenti">
            {recenti.map((x) => (
              <li key={x}><button type="button" onClick={() => { setQ(x); void chiedi(x); }}>{x}</button></li>
            ))}
          </ul>
        </section>
      )}

      {res?.modalita === "testuale" && <p className="nota-piccola">Solo per parole: il motore di analisi non risponde.</p>}

      <section className="registro" aria-live="polite">
        {vuota && !risp && <p className="vuoto">Nessun risultato.</p>}

        {res && res.concetti.length > 0 && (
          <>
            <h3 className="sezione">Argomenti</h3>
            <ul className="vicini">
              {res.concetti.map((c) => (
                <li key={c.id}>
                  <a className="tema-chip" href={`/mappa/?c=${c.id}`} title={c.ramo ? `in ${c.ramo}` : undefined}
                    style={{ "--c": coloreTinta(c.tinta) } as React.CSSProperties}>
                    <i aria-hidden="true" />{c.nome}<span className="indice-n">{c.note}</span>
                  </a>
                </li>
              ))}
            </ul>
          </>
        )}

        {res && res.azioni.length > 0 && (
          <>
            <h3 className="sezione">Da fare</h3>
            <Compiti onSpunta={(c) => { const a = res.azioni.find((x) => x.id === c.id); if (a) spunta(a); }}
              voci={res.azioni.map((a) => ({ id: a.id, testo: a.testo, stato: a.stato, quando: a.quando, chi: a.chi,
                href: `/nota/?id=${a.nota.id}` }))} />
          </>
        )}

        {res && res.risultati.length > 0 && (
          <>
            <h3 className="sezione">Note</h3>
            <ul className="risultati">
              {res.risultati.map((r) => (
                <li key={r.entry_id}>
                  <a href={linkNota(r.entry_id, r.secondi)}>
                    <span className="voce-titolo">{r.titolo ?? "Nota senza titolo"}</span>
                    <span className="risultato-meta">
                      {r.rami.map((a) => <i key={a.nome} className="punto" title={a.nome}
                        style={{ "--c": coloreTinta(a.tinta) } as React.CSSProperties} />)}
                      {formatWhen(r.data_ora ?? r.creato_il)}{r.per_significato ? " · per significato" : ""}
                    </span>
                    {r.frammento ? <span className="frammento"><Frammento testo={r.frammento} /></span>
                      : r.executive_summary?.[0] ? <span className="frammento">{r.executive_summary[0]}</span> : null}
                  </a>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </Shell>
  );
}
