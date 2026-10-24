"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Pulsante da tenere premuto: l'azione parte solo dopo `ms` millisecondi di
 * pressione continua. Lasciare prima, o spostare il dito fuori, annulla.
 * Funziona con dito, mouse e tastiera (Spazio o Invio tenuti premuti).
 */
export function HoldButton({ label, hint, onConfirm, ms = 2000, className = "" }: {
  label: string;
  hint: string;
  onConfirm: () => void;
  ms?: number;
  className?: string;
}) {
  const [progress, setProgress] = useState(0);
  const raf = useRef(0);
  const start = useRef<number | null>(null);

  useEffect(() => () => cancelAnimationFrame(raf.current), []);

  function begin() {
    if (start.current !== null) return;
    start.current = performance.now();
    navigator.vibrate?.(10);
    const tick = () => {
      if (start.current === null) return;
      const p = Math.min(1, (performance.now() - start.current) / ms);
      setProgress(p);
      if (p >= 1) {
        start.current = null;
        navigator.vibrate?.([30, 40, 30]);
        setProgress(0);
        onConfirm();
        return;
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
  }

  function cancel() {
    cancelAnimationFrame(raf.current);
    start.current = null;
    setProgress(0);
  }

  return (
    <button
      type="button"
      className={`tieni ${className}`}
      data-attivo={progress > 0}
      style={{ "--p": progress } as React.CSSProperties}
      aria-label={`${label}: ${hint}`}
      onPointerDown={(e) => { e.preventDefault(); begin(); }}
      onPointerUp={cancel}
      onPointerLeave={cancel}
      onPointerCancel={cancel}
      onContextMenu={(e) => e.preventDefault()}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); begin(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") cancel(); }}
      onBlur={cancel}
    >
      <span className="tieni-testo">{label}</span>
      <span className="tieni-aiuto" aria-hidden="true">{hint}</span>
    </button>
  );
}
