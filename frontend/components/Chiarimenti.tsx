"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { dataBreve } from "@/lib/grafo";

/** Le domande del sistema quando non è sicuro di dove va qualcosa (o se un nome è un tema già noto).
 *  Si risponde con un tocco su un'opzione, oppure a parole: la risposta diventa un'indicazione che il
 *  sistema rilegge ogni volta, così la stessa domanda non torna. */

export type Chiarimento = {
  id: string; tipo: "collocazione" | "nome"; domanda: string; argomento: string | null; punti: string[];
  opzioni: { n: number; etichetta: string }[];
  nota: { id: string; titolo: string | null; data: string | null };
};

type Stato = { fatto?: string; errore?: string; invio?: boolean };

export function Chiarimenti({ voci, conNota = true, onRisposto }: {
  voci: Chiarimento[];
  conNota?: boolean;
  onRisposto?: (c: Chiarimento) => void;
}) {
  const [stato, setStato] = useState<Record<string, Stato>>({});
  const [aperto, setAperto] = useState<string | null>(null);
  const [testo, setTesto] = useState("");

  async function rispondi(c: Chiarimento, corpo: { scelta: number } | { testo: string }, detto: string) {
    setStato((s) => ({ ...s, [c.id]: { invio: true } }));
    try {
      const r = await api<{ applicata: boolean; ricolloca: boolean }>(`/api/v1/chiarimenti/${c.id}`,
        { method: "POST", json: corpo });
      setStato((s) => ({ ...s, [c.id]: { fatto: r.applicata ? `Fatto: ${detto}` : "Ricevuto: lo rimetto al suo posto…" } }));
      setAperto(null);
      setTesto("");
      onRisposto?.(c);
    } catch (e) {
      setStato((s) => ({ ...s, [c.id]: { errore: e instanceof Error ? e.message : String(e) } }));
    }
  }

  if (voci.length === 0) return null;
  return (
    <ul className="chiarimenti">
      {voci.map((c) => {
        const st = stato[c.id] ?? {};
        return (
          <li key={c.id} className="chiarimento" data-fatto={st.fatto ? "" : undefined}>
            <p className="chiarimento-domanda">{c.domanda}</p>
            {conNota && (
              <p className="chiarimento-nota">
                <a href={`/nota/?id=${c.nota.id}`}>{c.nota.titolo ?? "nota"}</a>
                {c.nota.data && <> · {dataBreve(c.nota.data)}</>}
              </p>
            )}
            {c.punti.length > 0 && (
              <ul className="chiarimento-punti">
                {c.punti.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            )}
            {st.fatto ? <p className="chiarimento-esito" role="status">{st.fatto}</p> : (
              <>
                <div className="chiarimento-opzioni" role="group" aria-label="Risposte possibili">
                  {c.opzioni.map((o) => (
                    <button key={o.n} type="button" className="opzione" disabled={st.invio}
                      onClick={() => void rispondi(c, { scelta: o.n }, o.etichetta)}>{o.etichetta}</button>
                  ))}
                  <button type="button" className="opzione altro" aria-expanded={aperto === c.id} disabled={st.invio}
                    onClick={() => { setAperto(aperto === c.id ? null : c.id); setTesto(""); }}>altro…</button>
                </div>
                {aperto === c.id && (
                  <form className="chiarimento-testo" onSubmit={(ev) => {
                    ev.preventDefault();
                    if (testo.trim()) void rispondi(c, { testo: testo.trim() }, testo.trim());
                  }}>
                    <label className="nascosto" htmlFor={`risp-${c.id}`}>La tua risposta</label>
                    <textarea id={`risp-${c.id}`} rows={2} maxLength={500} value={testo} autoFocus
                      placeholder={c.tipo === "nome" ? "es. sì, è quello di prima" : "es. fa parte di… / è il sito in…"}
                      onChange={(ev) => setTesto(ev.target.value)} />
                    <button className="primario" type="submit" disabled={!testo.trim() || st.invio}>Invia</button>
                  </form>
                )}
                {st.errore && <p className="avviso grave" role="alert">{st.errore}</p>}
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}
