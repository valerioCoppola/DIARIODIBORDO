"use client";

import { useRef } from "react";
import { etichettaMese, grigliaMese, isoGiorno, type Calendario as Dati } from "@/lib/archivio";

const SETTIMANA = ["L", "M", "M", "G", "V", "S", "D"];
const SETTIMANA_ESTESA = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"];

/**
 * Mese a griglia. Ogni giorno con vocali porta un pallino (uno per vocale, fino a tre).
 * Si cambia mese con le frecce o scorrendo il dito sulla griglia.
 */
export function Calendario({ mese, dati, selezionato, onMese, onGiorno }: {
  mese: Date;
  dati: Dati | null;
  selezionato: string;
  onMese: (delta: number) => void;
  onGiorno: (g: string) => void;
}) {
  const oggi = isoGiorno(new Date());
  const celle = grigliaMese(mese);
  const inizio = useRef<{ x: number; y: number } | null>(null);

  return (
    <section className="calendario" aria-label="Calendario">
      <div className="cal-testa">
        <button type="button" className="cal-freccia" onClick={() => onMese(-1)} aria-label="Mese precedente">‹</button>
        <h2 aria-live="polite">{etichettaMese(mese)}</h2>
        <button type="button" className="cal-freccia" onClick={() => onMese(1)} aria-label="Mese successivo">›</button>
      </div>
      <div
        className="cal-griglia"
        role="grid"
        onPointerDown={(e) => { inizio.current = { x: e.clientX, y: e.clientY }; }}
        onPointerUp={(e) => {
          const s = inizio.current;
          inizio.current = null;
          if (!s) return;
          const dx = e.clientX - s.x;
          if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(e.clientY - s.y) * 1.5) onMese(dx < 0 ? 1 : -1);
        }}
      >
        {SETTIMANA.map((d, i) => (
          <span key={i} className="cal-sett" role="columnheader" aria-label={SETTIMANA_ESTESA[i]}>{d}</span>
        ))}
        {celle.map((g, i) => {
          if (!g) return <span key={i} className="cal-vuoto" />;
          const info = dati?.giorni[g];
          const n = info?.vocali ?? 0;
          const giorno = Number(g.slice(8));
          return (
            <button
              key={g}
              type="button"
              className="cal-giorno"
              data-oggi={g === oggi}
              data-scelto={g === selezionato}
              data-pieno={n > 0}
              data-futuro={g > oggi}
              onClick={() => onGiorno(g)}
              aria-pressed={g === selezionato}
              aria-label={`${giorno}${n ? `, ${n === 1 ? "1 vocale" : `${n} vocali`}` : ""}`}
            >
              <span className="cal-num">{giorno}</span>
              <span className="cal-punti" aria-hidden="true">
                {Array.from({ length: Math.min(n, 3) }, (_, k) => <i key={k} />)}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
