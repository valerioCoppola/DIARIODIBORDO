// Il diario aperto in questo browser: nome e copertina (per disegnare subito la pagina
// giusta), la pagina a cui è stato chiuso (il segno del nastro) e la chiusura.
// Il diario aperto vero è sul PC, nella sessione: questo è solo un ricordo locale.

import { api } from "./api";

export type Copertina = "cuoio" | "notte" | "bosco" | "vino" | "ardesia" | "senape";
export type DiarioCorrente = { id: string; nome: string; copertina: Copertina };

export const COPERTINE: { id: Copertina; nome: string }[] = [
  { id: "cuoio", nome: "Cuoio" },
  { id: "notte", nome: "Blu notte" },
  { id: "bosco", nome: "Verde bosco" },
  { id: "vino", nome: "Vino" },
  { id: "ardesia", nome: "Ardesia" },
  { id: "senape", nome: "Senape" },
];

const K_DIARIO = "ddb-diario";
const K_COPERTINA = "ddb-copertina";
const K_SEGNO = "ddb-segno:";
export const EVENTO = "ddb-diario";

function leggi(k: string): string | null {
  try { return localStorage.getItem(k); } catch { return null; }
}
function scrivi(k: string, v: string | null) {
  try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* privato */ }
}

export function diarioInCache(): DiarioCorrente | null {
  try { return JSON.parse(leggi(K_DIARIO) ?? "null"); } catch { return null; }
}

/** Ricorda il diario aperto e veste la pagina con la sua copertina. */
export function ricordaDiario(d: DiarioCorrente | null) {
  scrivi(K_DIARIO, d ? JSON.stringify(d) : null);
  vestiCopertina(d?.copertina ?? null);
  window.dispatchEvent(new CustomEvent(EVENTO, { detail: d }));
}

/** Il cuoio intorno alla pagina prende il colore del diario ("scaffale" fuori dai diari). */
export function vestiCopertina(c: Copertina | "scaffale" | null) {
  const h = document.documentElement;
  if (c && c !== "cuoio") h.dataset.copertina = c;
  else delete h.dataset.copertina;
  if (c !== "scaffale") scrivi(K_COPERTINA, c);
}

/** Il segno del nastro: ogni diario si riapre alla pagina in cui è stato chiuso. */
export function paginaSegnata(id: string): string {
  const p = leggi(K_SEGNO + id);
  return p && p.startsWith("/") && !p.startsWith("//") ? p : "/";
}

export function segnaPagina(id: string, path: string) {
  scrivi(K_SEGNO + id, path);
}

// Una pagina può impedire la chiusura (es. durante una registrazione) dicendo perché.
let guardia: (() => string | null) | null = null;
export function guardiaChiusura(fn: (() => string | null) | null) {
  guardia = fn;
}
export function motivoPerNonChiudere(): string | null {
  return guardia?.() ?? null;
}

/** Chiude il diario sul PC e qui; il segno resta alla pagina corrente. */
export async function chiudiDiario(): Promise<void> {
  const d = diarioInCache();
  if (d) segnaPagina(d.id, location.pathname + location.search);
  try {
    await api("/api/v1/diari/chiudi", { method: "POST" });
  } finally {
    // La copertina resta quella del diario fino allo scaffale: la sta chiudendo l'animazione.
    scrivi(K_DIARIO, null);
  }
}

export async function apriDiario(d: DiarioCorrente): Promise<string> {
  await api(`/api/v1/diari/${d.id}/apri`, { method: "POST" });
  ricordaDiario(d);
  return paginaSegnata(d.id);
}
