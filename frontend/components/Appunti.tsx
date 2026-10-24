"use client";

import { coloreTinta } from "@/lib/grafo";

/** Gli appunti di un vocale: per ogni argomento (il suo tema) i punti in ordine, con i dettagli sotto.
 *  Le cose da fare hanno la casella al posto del numero. Per riascoltare si tocca la trascrizione. */

export type Azione = {
  id: string; stato: "aperta" | "completata" | "annullata"; quando: string | null; ribadita: number;
  chiusa_da: { id: string; titolo: string | null } | null;
};
export type Voce = {
  id: string; tipo: "punto" | "idea" | "decisione" | "domanda" | "fatto" | "da_fare"; testo: string;
  stato: "attivo" | "superato"; secondi: number | null; azione: Azione | null; dettagli: Voce[];
};
export type BloccoAppunti = { tema: { id: string; nome: string; tinta: number | null } | null; punti: Voce[] };

const SEGNO: Partial<Record<Voce["tipo"], string>> = { decisione: "deciso", domanda: "da capire", idea: "idea" };

type Props = {
  href?: (v: Voce) => string | undefined;             // vai alla nota (nella scheda del tema)
  onSpunta: (a: Azione) => void;
};

function Riga({ v, href, onSpunta }: { v: Voce } & Props) {
  const segno = SEGNO[v.tipo];
  const meta = v.azione ? [v.azione.quando, v.azione.ribadita ? `detta ${v.azione.ribadita + 1} volte` : null]
    .filter(Boolean).join(" · ") : "";
  const link = href?.(v);
  const testo = <>{segno && <span className="segno">{segno}</span>}{v.testo}</>;
  return (
    <>
      {v.azione && (
        <input type="checkbox" className="spunta" checked={v.azione.stato === "completata"} aria-label={v.testo}
          onChange={() => onSpunta(v.azione!)} />
      )}
      <span className="voce-corpo">
        {link ? <a className="voce-testo" href={link}>{testo}</a>
          : <span className="voce-testo">{testo}</span>}
        {meta && <span className="quando"> {meta}</span>}
        {v.azione?.chiusa_da && v.azione.stato !== "aperta" && (
          <a className="chiusa-da" href={`/nota/?id=${v.azione.chiusa_da.id}`}>fatta · {v.azione.chiusa_da.titolo}</a>
        )}
      </span>
    </>
  );
}

export function ElencoPunti({ punti, ...props }: { punti: Voce[] } & Props) {
  return (
    <ol className="elenco">
      {punti.map((p) => (
        <li key={p.id} data-fare={p.azione ? "" : undefined} data-stato={p.azione?.stato}
          data-superato={p.stato === "superato" || undefined}>
          <Riga v={p} {...props} />
          {p.dettagli.length > 0 && (
            <ul className="dettagli-elenco">
              {p.dettagli.map((d) => (
                <li key={d.id} data-fare={d.azione ? "" : undefined} data-stato={d.azione?.stato}
                  data-superato={d.stato === "superato" || undefined}>
                  <Riga v={d} {...props} />
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}

export function Appunti({ blocchi, ...props }: { blocchi: BloccoAppunti[] } & Props) {
  return (
    <div className="appunti">
      {blocchi.map((b, i) => (
        <section key={b.tema?.id ?? `b${i}`} className="blocco"
          style={{ "--c": coloreTinta(b.tema?.tinta) } as React.CSSProperties}>
          {b.tema && (
            <a className="blocco-tema" href={`/mappa/?c=${b.tema.id}`}><i aria-hidden="true" />{b.tema.nome}</a>
          )}
          <ElencoPunti punti={b.punti} {...props} />
        </section>
      ))}
    </div>
  );
}
