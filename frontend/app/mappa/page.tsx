"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ElencoPunti, type Voce } from "@/components/Appunti";
import { Arcipelago } from "@/components/Arcipelago";
import { Chiarimenti, type Chiarimento } from "@/components/Chiarimenti";
import { Compiti, linkNota, type Compito } from "@/components/Compiti";
import { Shell, useSession } from "@/components/Shell";
import { api } from "@/lib/api";
import {
  coloreTema, dataBreve, Grafo, primaFrase, type AzioneG, type Concetto, type DatiGrafo, type TipoTema,
} from "@/lib/grafo";

type Posto = { concetto: string | null };
type Indicazione = { id: string; testo: string; nota: string | null; data: string };

type NotaRif = { id: string; titolo: string | null; data: string | null };
type AzioneT = {
  id: string; testo: string; chi: string | null; quando: string | null; stato: "aperta" | "completata" | "annullata";
  nota: NotaRif; ribadita: number; secondi: number | null; tema: { id: string; nome: string | null } | null;
};
type SchedaTema = {
  id: string; nome: string; tipo: TipoTema | null; sintesi: string | null; aggiornato: string | null;
  tinta: number; provvisorio: boolean; fissato: boolean; percorso: { id: string; nome: string }[];
  sotto: { id: string; nome: string; tipo: TipoTema | null; sintesi: string | null; aperte: number }[];
  azioni: AzioneT[];
  appunti: { nota: NotaRif; punti: Voce[] }[];
};

function leggiUrl(): Posto {
  const p = new URLSearchParams(window.location.search);
  const n = p.get("n");
  // Indirizzi della mappa di prima: n=c:<id> è un argomento, a=<id> un'area (che ora non esiste più).
  const concetto = n?.startsWith("c:") ? n.slice(2) : p.get("c") ?? p.get("a");
  return { concetto: concetto || null };
}

function url(p: Posto): string {
  return p.concetto ? `/mappa/?c=${p.concetto}` : "/mappa/";
}

const conta = (n: number, uno: string, tanti: string) => `${n} ${n === 1 ? uno : tanti}`;

export default function MappaPage() {
  const { me } = useSession();
  const [dati, setDati] = useState<DatiGrafo | null>(null);
  const [errore, setErrore] = useState<string | null>(null);
  const [posto, setPosto] = useState<Posto>({ concetto: null });
  const [tutte, setTutte] = useState(false);
  const [fontPronti, setFontPronti] = useState(false);
  const [scheda, setScheda] = useState<SchedaTema | null>(null);
  const [domande, setDomande] = useState<Chiarimento[]>([]);
  const [indicazioni, setIndicazioni] = useState<Indicazione[] | null>(null);
  const [modifica, setModifica] = useState<"sposta" | "rinomina" | null>(null);
  const [nomeNuovo, setNomeNuovo] = useState("");
  const [erroreModifica, setErroreModifica] = useState<string | null>(null);

  const grafo = useMemo(() => (dati ? new Grafo(dati) : null), [dati]);

  const carica = useCallback(async () => {
    try {
      const d = await api<DatiGrafo>("/api/v1/grafo");
      setDati(d);
      setErrore(null);
      setDomande(d.da_chiarire ? await api<Chiarimento[]>("/api/v1/chiarimenti") : []);
    } catch (e) {
      setErrore(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const caricaIndicazioni = useCallback(async () => {
    try { setIndicazioni(await api<Indicazione[]>("/api/v1/indicazioni")); } catch { setIndicazioni([]); }
  }, []);

  useEffect(() => {
    if (me) void carica();
  }, [me, carica]);

  useEffect(() => {
    const n = new URLSearchParams(window.location.search).get("n");
    if (n?.startsWith("n:")) window.location.replace(`/nota/?id=${n.slice(2)}`);
    const leggi = () => setPosto(leggiUrl());
    leggi();
    window.addEventListener("popstate", leggi);
    void document.fonts?.ready.then(() => setFontPronti(true));
    return () => window.removeEventListener("popstate", leggi);
  }, []);

  useEffect(() => {
    const n = new URLSearchParams(window.location.search).get("n");
    if (grafo && n?.startsWith("a:")) {
      const a = grafo.azioni.get(n.slice(2));
      window.location.replace(a ? `/nota/?id=${a.nota}` : "/mappa/");
    }
  }, [grafo]);

  const caricaScheda = useCallback(async (id: string) => {
    try {
      setScheda(await api<SchedaTema>(`/api/v1/temi/${id}`));
    } catch {
      setScheda(null);
    }
  }, []);

  useEffect(() => {
    if (!me || !posto.concetto) {
      setScheda(null);
      return;
    }
    setScheda((s) => (s?.id === posto.concetto ? s : null));
    void caricaScheda(posto.concetto);
  }, [me, posto.concetto, caricaScheda]);

  const vai = useCallback((p: Posto) => {
    window.history.pushState(null, "", url(p));
    setPosto(p);
    setTutte(false);
    setModifica(null);
    setErroreModifica(null);
    if (p.concetto) window.scrollTo({ top: 0 });
  }, []);

  async function spunta(id: string, stato: string) {
    const nuovo = stato === "completata" ? "aperta" : "completata";
    if (dati) setDati({ ...dati, azioni: dati.azioni.map((x) => (x.id === id ? { ...x, stato: nuovo } : x)) });
    setScheda((s) => s && { ...s, azioni: s.azioni.map((x) => (x.id === id ? { ...x, stato: nuovo } : x)) });
    try {
      await api(`/api/v1/action-items/${id}`, { method: "PATCH", json: { stato: nuovo } });
      if (posto.concetto) void caricaScheda(posto.concetto);
    } catch {
      await carica();
    }
  }

  if (!me) return <Shell active="mappa" />;

  const concetto = grafo && posto.concetto ? grafo.concetti.get(posto.concetto) ?? null : null;
  const antenati = grafo && concetto ? grafo.percorso(concetto.id) : [];

  function su() {
    vai({ concetto: concetto?.padre ?? null });
  }

  /** Correzioni a mano su un tema: spostarlo, rinominarlo, confermarlo. */
  async function correggi(id: string, azione: "sposta" | "rinomina" | "conferma", corpo?: object) {
    setErroreModifica(null);
    try {
      const r = await api<{ id?: string | null }>(`/api/v1/temi/${id}/${azione}`, { method: "POST", json: corpo ?? {} });
      setModifica(null);
      await carica();
      const dove = r.id ?? id;
      if (dove !== posto.concetto) vai({ concetto: dove });
      else void caricaScheda(dove);
      if (indicazioni) void caricaIndicazioni();
    } catch (e) {
      setErroreModifica(e instanceof Error ? e.message : String(e));
    }
  }

  const compito = (a: AzioneG): Compito => ({
    id: a.id, testo: a.testo, stato: a.stato, quando: a.quando, chi: a.chi, ribadita: a.ribadita,
    href: `/nota/?id=${a.nota}`,
  });

  /** Cose da fare raggruppate per tema: il nome del tema una volta, sotto le sue righe. */
  const daFare = (azioni: AzioneG[]) => {
    if (!grafo || azioni.length === 0) return null;
    const gruppi = grafo.perTema(azioni);
    const visibili = tutte ? gruppi : gruppi.slice(0, 4);
    return (
      <section aria-labelledby="mappa-da-fare">
        <h3 id="mappa-da-fare">Da fare</h3>
        {visibili.map((g) => (
          <div key={g.tema?.id ?? "-"} className="gruppo-fare"
            style={{ "--c": coloreTema(g.tema) } as React.CSSProperties}>
            {g.tema
              ? <button type="button" className="gruppo-tema" onClick={() => vai({ concetto: g.tema!.id })}>
                  <i aria-hidden="true" />{g.tema.nome}
                </button>
              : gruppi.length > 1 && <span className="gruppo-tema"><i aria-hidden="true" />altro</span>}
            <Compiti voci={(tutte ? g.azioni : g.azioni.slice(0, 4)).map(compito)}
              onSpunta={(c) => void spunta(c.id, c.stato)} />
          </div>
        ))}
        {(gruppi.length > 4 || gruppi.some((g) => g.azioni.length > 4)) && (
          <button type="button" className="altri-rami mc" onClick={() => setTutte((v) => !v)}>
            {tutte ? "meno" : `tutte (${azioni.filter((a) => a.stato === "aperta").length})`}
          </button>
        )}
      </section>
    );
  };

  /** L'indice dei temi: ogni tema con i suoi sotto-temi rientrati. */
  const indice = (temi: Concetto[], livello = 0, conSotto = true): React.ReactNode => (
    <ul className="indice" data-livello={livello}>
      {temi.map((c) => {
        const sotto = grafo!.sottoTemi(c.id);
        const aperte = grafo!.apertePerTema(c.id);
        return (
          <li key={c.id}>
            <button type="button" onClick={() => vai({ concetto: c.id })}
              style={{ "--c": coloreTema(c) } as React.CSSProperties} data-provvisorio={c.provvisorio || undefined}>
              <span className="tema-nome">{c.nome}</span>
              <span className="tema-quando">{dataBreve(grafo!.ultima(c.id))}</span>
              {c.sintesi && livello === 0 && <span className="tema-stato">{primaFrase(c.sintesi)}</span>}
              {aperte > 0 && <span className="tema-extra">{conta(aperte, "da fare", "da fare")}</span>}
            </button>
            {conSotto && sotto.length > 0 && indice(sotto, livello + 1)}
          </li>
        );
      })}
    </ul>
  );

  return (
    <Shell active="mappa">
      {concetto && grafo && (
        <nav className="briciole" aria-label="Percorso">
          <button type="button" onClick={() => vai({ concetto: null })}>Mappa</button>
          {antenati.map((x) => (
            <span key={x.id} className="briciola">
              <span className="sep" aria-hidden="true">›</span>
              <button type="button" onClick={() => vai({ concetto: x.id })}>{x.nome}</button>
            </span>
          ))}
          <span className="sep" aria-hidden="true">›</span>
          <span aria-current="location">{concetto.nome}</span>
        </nav>
      )}

      {errore && <p className="avviso grave" role="alert">{errore}</p>}
      {!grafo && !errore && <p className="vuoto">Caricamento…</p>}
      {grafo && grafo.note.size === 0 && <p className="vuoto">Ancora nessuna nota.</p>}
      {grafo && posto.concetto && !concetto && <p className="vuoto">Non c’è più: forse è stato eliminato.</p>}

      {grafo && grafo.note.size > 0 && grafo.concetti.size > 0 && (
        <Arcipelago key={String(fontPronti)} grafo={grafo} fuoco={concetto?.id ?? null}
          onConcetto={(id) => vai({ concetto: id })} onSu={su} />
      )}

      {/* Tema: a che punto è, sotto-temi, cosa resta da fare, gli appunti nel tempo */}
      {grafo && concetto && (() => {
        const note = grafo.noteTutte(concetto.id);
        const vicini = grafo.vicini(concetto.id);
        const s = scheda?.id === concetto.id ? scheda : null;
        const sintesi = s?.sintesi ?? concetto.sintesi;
        const tipo = s?.tipo ?? concetto.tipo;
        const sotto = grafo.sottoTemi(concetto.id);
        return (
          <article className="scheda tema-pagina" style={{ "--c": coloreTema(concetto) } as React.CSSProperties}>
            <header>
              <h2 className="tema-titolo">{concetto.nome}</h2>
              <p className="scheda-meta">
                {tipo && tipo !== "tema" && <>{tipo} · </>}
                {conta(note.length, "nota", "note")}
                {note.length > 1 && <> · dal {dataBreve(note[note.length - 1].data)}</>}
                {sotto.length > 0 && <> · {conta(sotto.length, "parte", "parti")}</>}
              </p>
              {concetto.provvisorio && (
                <p className="provvisorio">
                  Da confermare: è nato da un vocale di cui non ero sicuro.{" "}
                  <button type="button" className="collegamento" onClick={() => void correggi(concetto.id, "conferma")}>
                    va bene così
                  </button>
                </p>
              )}
              <p className="tema-comandi">
                <button type="button" className="collegamento" aria-expanded={modifica === "sposta"}
                  onClick={() => { setModifica(modifica === "sposta" ? null : "sposta"); setErroreModifica(null); }}>
                  sposta
                </button>
                <button type="button" className="collegamento" aria-expanded={modifica === "rinomina"}
                  onClick={() => { setModifica(modifica === "rinomina" ? null : "rinomina"); setNomeNuovo(concetto.nome);
                    setErroreModifica(null); }}>
                  rinomina
                </button>
              </p>
              {modifica === "sposta" && (
                <div className="tema-modifica">
                  <label htmlFor="sposta-in">Fa parte di</label>
                  <select id="sposta-in" defaultValue={concetto.padre ?? ""}
                    onChange={(ev) => void correggi(concetto.id, "sposta", { dentro: ev.target.value || null })}>
                    <option value="">nessuno: è un tema a sé</option>
                    {grafo.destinazioni(concetto.id).map((x) => (
                      <option key={x.id} value={x.id}>{"\u00a0\u00a0".repeat(grafo.percorso(x.id).length)}{x.nome}</option>
                    ))}
                  </select>
                </div>
              )}
              {modifica === "rinomina" && (
                <form className="tema-modifica" onSubmit={(ev) => {
                  ev.preventDefault();
                  if (nomeNuovo.trim()) void correggi(concetto.id, "rinomina", { nome: nomeNuovo.trim() });
                }}>
                  <label htmlFor="nome-tema">Nome</label>
                  <input id="nome-tema" value={nomeNuovo} maxLength={40} autoComplete="off" autoFocus
                    onChange={(ev) => setNomeNuovo(ev.target.value)} />
                  <button className="primario" type="submit" disabled={!nomeNuovo.trim()}>Salva</button>
                </form>
              )}
              {erroreModifica && <p className="avviso grave" role="alert">{erroreModifica}</p>}
            </header>

            {sintesi && <p className="stato-tema">{sintesi}</p>}

            {s && s.sotto.length > 0 && (
              <section aria-labelledby="tema-sotto">
                <h3 id="tema-sotto">Dentro</h3>
                <ul className="indice">
                  {s.sotto.map((x) => (
                    <li key={x.id}>
                      <button type="button" onClick={() => vai({ concetto: x.id })}
                        style={{ "--c": coloreTema(grafo.concetti.get(x.id)) } as React.CSSProperties}>
                        <span className="tema-nome">{x.nome}</span>
                        <span className="tema-quando">{dataBreve(grafo.ultima(x.id))}</span>
                        {x.sintesi && <span className="tema-stato">{primaFrase(x.sintesi)}</span>}
                        {x.aperte > 0 && <span className="tema-extra">{conta(x.aperte, "da fare", "da fare")}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {s && s.azioni.length > 0 && (
              <section aria-labelledby="tema-fare">
                <h3 id="tema-fare">Da fare</h3>
                <Compiti onSpunta={(c) => void spunta(c.id, c.stato)} voci={s.azioni.map((x) => ({
                  id: x.id, testo: x.testo, stato: x.stato, quando: x.quando ?? x.tema?.nome ?? null, chi: x.chi,
                  ribadita: x.ribadita, href: linkNota(x.nota.id, x.secondi),
                }))} />
              </section>
            )}

            {s && s.appunti.length > 0 && (
              <section aria-labelledby="tema-appunti">
                <h3 id="tema-appunti">Appunti</h3>
                <ol className="cronaca">
                  {s.appunti.map((x) => (
                    <li key={x.nota.id}>
                      <span className="cronaca-data">{dataBreve(x.nota.data)}</span>
                      <div className="cronaca-voce">
                        <a className="voce-titolo" href={`/nota/?id=${x.nota.id}`}>{x.nota.titolo}</a>
                        <ElencoPunti punti={x.punti} onSpunta={(az) => void spunta(az.id, az.stato)}
                          href={(v) => linkNota(x.nota.id, v.secondi)} />
                      </div>
                    </li>
                  ))}
                </ol>
              </section>
            )}

            {vicini.length > 0 && (
              <section aria-labelledby="tema-vicini">
                <h3 id="tema-vicini">Se ne parla insieme a</h3>
                <ul className="vicini">
                  {vicini.slice(0, 16).map((v) => (
                    <li key={v.concetto.id}>
                      <button type="button" className="tema-chip"
                        style={{ "--c": coloreTema(v.concetto) } as React.CSSProperties}
                        onClick={() => vai({ concetto: v.concetto.id })}>
                        <i aria-hidden="true" />{v.concetto.nome}
                        <span className="indice-n">{v.peso}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </article>
        );
      })()}

      {grafo && grafo.note.size > 0 && !posto.concetto && (
        <div className="scheda">
          {domande.length > 0 && (
            <section aria-labelledby="mappa-chiarire">
              <h3 id="mappa-chiarire">Da chiarire</h3>
              <p className="nota-piccola">Non ero sicuro di dove mettere queste cose: una risposta e le sistemo.</p>
              <Chiarimenti voci={domande} onRisposto={() => { setTimeout(() => void carica(), 1500); }} />
            </section>
          )}
          {daFare(grafo.aperte())}
          {grafo.inMovimento(21, 4).length > 0 && (
            <section aria-labelledby="mappa-movimento">
              <h3 id="mappa-movimento">In movimento</h3>
              {indice(grafo.inMovimento(21, 4), 0, false)}
            </section>
          )}
          <section aria-labelledby="mappa-indice">
            <h3 id="mappa-indice">Indice</h3>
            {indice(grafo.radici())}
          </section>
          <details className="indicazioni" onToggle={(ev) => {
            if ((ev.target as HTMLDetailsElement).open && indicazioni === null) void caricaIndicazioni();
          }}>
            <summary>Cosa mi hai detto</summary>
            <p className="nota-piccola">Le tue risposte e correzioni: le rileggo ogni volta che metto in ordine un vocale.</p>
            {indicazioni === null ? <p className="vuoto">Caricamento…</p>
              : indicazioni.length === 0 ? <p className="vuoto">Ancora niente.</p> : (
                <ul className="elenco-indicazioni">
                  {indicazioni.map((x) => (
                    <li key={x.id}>
                      <span>{x.testo}</span>
                      <button type="button" className="collegamento" aria-label={`Togli: ${x.testo}`}
                        onClick={async () => {
                          await api(`/api/v1/indicazioni/${x.id}`, { method: "DELETE" });
                          void caricaIndicazioni();
                        }}>togli</button>
                    </li>
                  ))}
                </ul>
              )}
          </details>
        </div>
      )}
    </Shell>
  );
}
