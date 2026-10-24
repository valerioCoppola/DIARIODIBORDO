"use client";

/** Cose da fare, una per riga: casella, cosa, e solo se c'è quando / chi / quante volte è stata detta.
 *  Toccando il testo si va al punto del vocale in cui se ne parla. */

export type Compito = {
  id: string;
  testo: string;
  stato: "aperta" | "completata" | "annullata";
  quando?: string | null;
  chi?: string | null;
  ribadita?: number;
  href?: string;
  onApri?: () => void;
  chiusaDa?: { id: string; titolo: string | null } | null;
};

export function Compiti({ voci, onSpunta }: { voci: Compito[]; onSpunta: (c: Compito) => void }) {
  return (
    <ul className="compiti">
      {voci.map((c) => {
        const meta = [c.quando, c.chi, c.ribadita ? `detta ${c.ribadita + 1} volte` : null].filter(Boolean).join(" · ");
        return (
          <li key={c.id} data-stato={c.stato}>
            <input type="checkbox" className="spunta" checked={c.stato === "completata"} aria-label={c.testo}
              onChange={() => onSpunta(c)} />
            {c.href ? <a className="cosa" href={c.href}>{c.testo}</a>
              : c.onApri ? <button type="button" className="cosa" onClick={c.onApri}>{c.testo}</button>
              : <span className="cosa">{c.testo}</span>}
            {meta && <span className="quando">{meta}</span>}
            {c.chiusaDa && c.stato !== "aperta" && (
              <a className="chiusa-da" href={`/nota/?id=${c.chiusaDa.id}`}>fatta · {c.chiusaDa.titolo ?? "nota"}</a>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function linkNota(id: string, secondi?: number | null): string {
  return secondi != null && secondi > 0 ? `/nota/?id=${id}&t=${Math.floor(secondi)}` : `/nota/?id=${id}`;
}
