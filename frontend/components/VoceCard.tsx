"use client";

import { durata, link, ora, type Voce } from "@/lib/archivio";
import { FASE } from "@/lib/fasi";

/** Una riga dell'archivio: ora, titolo, prima frase della sintesi; lo stato solo se non è pronta. */
export function VoceCard({ v }: { v: Voce }) {
  const stato = v.job?.stato ?? "";
  const pronta = stato === "archiviazione_completata" && v.nota?.titolo;
  const titolo = v.nota?.titolo ?? (v.nome_file && !v.registrato_il ? v.nome_file : "Vocale");
  const meta = [durata(v.durata_secondi), pronta ? v.nota?.categoria : null].filter(Boolean).join(" · ");
  return (
    <li>
      <a className="voce-card" href={link(v)} data-pronta={pronta ? "si" : "no"}>
        <span className="voce-ora">{ora(v)}</span>
        <span className="voce-corpo">
          <span className="voce-titolo">{titolo}</span>
          {pronta && v.nota?.executive_summary?.[0] && <span className="voce-sintesi">{v.nota.executive_summary[0]}</span>}
          <span className="voce-meta">
            {meta}
            {!pronta && stato && (
              <span className="stato" data-s={stato}>{FASE[stato] ?? stato}</span>
            )}
          </span>
        </span>
      </a>
    </li>
  );
}
