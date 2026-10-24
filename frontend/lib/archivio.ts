// Tipi e utilità dell'archivio: giorni nel fuso del telefono, etichette in italiano.

export type Voce = {
  id: string;
  nome_file: string | null;
  durata_secondi: number | null;
  registrato_il: string | null;
  creato_il: string;
  giorno: string | null; // AAAA-MM-GG nel fuso del telefono
  audio_disponibile: boolean;
  job: { id: string; stato: string; errore: string | null; messaggio_errore: string | null } | null;
  nota: { id: string; titolo: string | null; categoria: string; executive_summary: string[] | null } | null;
};

export type Calendario = {
  mese: string;
  giorni: Record<string, { vocali: number; note: number; categorie: string[] }>;
  primo_giorno: string | null;
  ultimo_giorno: string | null;
};

export function fuso(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Rome";
  } catch {
    return "Europe/Rome";
  }
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Data locale AAAA-MM-GG. */
export function isoGiorno(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function daIso(g: string): Date {
  const [y, m, d] = g.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function isoMese(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

const fmtGiorno = new Intl.DateTimeFormat("it-IT", { weekday: "long", day: "numeric", month: "long" });
const fmtGiornoAnno = new Intl.DateTimeFormat("it-IT", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const fmtMese = new Intl.DateTimeFormat("it-IT", { month: "long", year: "numeric" });
const fmtOra = new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit" });

/** "Oggi", "Ieri" o "lunedì 19 ottobre" (con l'anno se non è quello corrente). */
export function etichettaGiorno(g: string): string {
  const oggi = isoGiorno(new Date());
  const ieri = isoGiorno(new Date(Date.now() - 86_400_000));
  const d = daIso(g);
  const testo = d.getFullYear() === new Date().getFullYear() ? fmtGiorno.format(d) : fmtGiornoAnno.format(d);
  if (g === oggi) return `Oggi · ${testo}`;
  if (g === ieri) return `Ieri · ${testo}`;
  return testo.charAt(0).toUpperCase() + testo.slice(1);
}

export function etichettaMese(d: Date): string {
  const t = fmtMese.format(d);
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export function ora(v: Voce): string {
  return fmtOra.format(new Date(v.registrato_il ?? v.creato_il));
}

export function durata(sec: number | null | undefined): string {
  if (sec == null) return "";
  const s = Math.round(sec);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${pad(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${pad(m % 60)} min`;
}

/** Vocali raggruppati per giorno, nell'ordine in cui arrivano (dal più recente). */
export function perGiorno(voci: Voce[]): { giorno: string; voci: Voce[] }[] {
  const out: { giorno: string; voci: Voce[] }[] = [];
  for (const v of voci) {
    const g = v.giorno ?? isoGiorno(new Date(v.registrato_il ?? v.creato_il));
    const last = out[out.length - 1];
    if (last && last.giorno === g) last.voci.push(v);
    else out.push({ giorno: g, voci: [v] });
  }
  return out;
}

/** Celle del mese, da lunedì: null per i giorni fuori dal mese. */
export function grigliaMese(mese: Date): (string | null)[] {
  const y = mese.getFullYear();
  const m = mese.getMonth();
  const primo = new Date(y, m, 1);
  const vuote = (primo.getDay() + 6) % 7; // lunedì = 0
  const giorni = new Date(y, m + 1, 0).getDate();
  const celle: (string | null)[] = Array(vuote).fill(null);
  for (let d = 1; d <= giorni; d++) celle.push(`${y}-${pad(m + 1)}-${pad(d)}`);
  while (celle.length % 7) celle.push(null);
  return celle;
}

export function link(v: Voce): string {
  return v.nota ? `/nota/?id=${v.nota.id}` : `/nota/?r=${v.id}`;
}
