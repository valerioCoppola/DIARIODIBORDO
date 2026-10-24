"use client";

import { useEffect, useState, type RefObject } from "react";

function mmss(s: number): string {
  const t = Math.max(0, Math.floor(s));
  const m = Math.floor(t / 60);
  return `${m}:${String(t % 60).padStart(2, "0")}`;
}

/** Comandi d'ascolto per un elemento <audio> della pagina: avvia/ferma, tratto che avanza, tempo. */
export function Lettore({ audio, durata, fisso = false }: {
  audio: RefObject<HTMLAudioElement | null>;
  durata?: number | null;
  fisso?: boolean;
}) {
  const [t, setT] = useState(0);
  const [d, setD] = useState(0);
  const [suona, setSuona] = useState(false);

  useEffect(() => {
    const a = audio.current;
    if (!a) return;
    const agg = () => {
      setT(a.currentTime);
      setD(Number.isFinite(a.duration) ? a.duration : 0);
      setSuona(!a.paused && !a.ended);
    };
    const eventi = ["timeupdate", "loadedmetadata", "durationchange", "play", "pause", "ended", "seeked"];
    eventi.forEach((ev) => a.addEventListener(ev, agg));
    agg();
    return () => eventi.forEach((ev) => a.removeEventListener(ev, agg));
  }, [audio]);

  const tot = d || durata || 0;
  const pct = tot ? Math.min(100, (t / tot) * 100) : 0;

  function avviaFerma() {
    const a = audio.current;
    if (!a) return;
    if (a.paused) void a.play().catch(() => {});
    else a.pause();
  }

  return (
    <section className="ascolta" aria-label="Ascolto" data-fisso={fisso}>
      <button type="button" className="suona" onClick={avviaFerma} aria-label={suona ? "Pausa" : "Ascolta"}>
        {suona ? (
          <svg viewBox="0 0 10 10" aria-hidden="true"><rect x="1.5" y="1" width="2.4" height="8" /><rect x="6.1" y="1" width="2.4" height="8" /></svg>
        ) : (
          <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 1 L9 5 L2 9 Z" /></svg>
        )}
      </button>
      <input
        className="traccia" type="range" min={0} max={tot || 1} step={0.1} value={Math.min(t, tot || 1)}
        style={{ "--v": `${pct}%` } as React.CSSProperties}
        aria-label="Posizione"
        onChange={(ev) => { const a = audio.current; if (a) a.currentTime = Number(ev.target.value); }}
      />
      <span className="tempo">{mmss(t)}{tot ? ` / ${mmss(tot)}` : ""}</span>
    </section>
  );
}
