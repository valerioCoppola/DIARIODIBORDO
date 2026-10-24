// La mappa dell'archivio: l'albero dei temi, le note, le cose da fare e i loro legami.
// Nessuna categoria fissa: in cima stanno i temi più ampi emersi da ciò che si è detto
// (sulla mappa, le isole), sotto le loro parti.

export type TipoTema = "progetto" | "idea" | "tema";
export type Concetto = {
  id: string; nome: string; tipo?: TipoTema | null; sintesi?: string | null; aggiornato?: string | null;
  padre: string | null; tinta: number; provvisorio?: boolean; fissato?: boolean; creato?: string | null;
};
export type NotaG = {
  id: string; titolo: string; categoria: string; data: string | null; sintesi: string | null; concetti: string[];
};
export type AzioneG = {
  id: string; nota: string; testo: string; chi: string | null; quando: string | null;
  stato: "aperta" | "completata" | "annullata"; tema: string | null; ribadita: number;
};
export type DatiGrafo = { concetti: Concetto[]; note: NotaG[]; azioni: AzioneG[]; da_chiarire?: number };

/** Il colore di un tema, dalla sua tinta (calcolata dal PC sulla forma dell'albero).
 *  Luminosità e saturazione vengono dal tema chiaro o scuro (--t-l, --t-c). */
export function coloreTinta(h: number | null | undefined): string {
  return h == null ? "var(--neutro)" : `oklch(var(--t-l) var(--t-c) ${Math.round(h)})`;
}

export function coloreTema(c: { tinta?: number | null } | null | undefined): string {
  return coloreTinta(c?.tinta);
}

const fmtData = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short", year: "numeric" });
const fmtDataBreve = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short" });
export function dataBreve(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.getFullYear() === new Date().getFullYear() ? fmtDataBreve.format(d) : fmtData.format(d);
}

export class Grafo {
  concetti = new Map<string, Concetto>();
  note = new Map<string, NotaG>();
  azioni = new Map<string, AzioneG>();
  notePerConcetto = new Map<string, NotaG[]>();
  azioniPerNota = new Map<string, AzioneG[]>();
  /** Quante note hanno in comune due argomenti. */
  private insieme = new Map<string, Map<string, number>>();

  /** Sotto-temi di ogni tema. */
  figli = new Map<string, Concetto[]>();
  daChiarire = 0;

  constructor(d: DatiGrafo) {
    this.daChiarire = d.da_chiarire ?? 0;
    const ids = new Set(d.concetti.map((c) => c.id));
    for (const c of d.concetti) {
      this.concetti.set(c.id, { ...c, padre: c.padre && ids.has(c.padre) && c.padre !== c.id ? c.padre : null });
    }
    // Un ciclo, se mai ci fosse, si spezza.
    for (const c of this.concetti.values()) {
      const visti = new Set<string>([c.id]);
      let p = c.padre ? this.concetti.get(c.padre) : undefined;
      while (p) {
        if (visti.has(p.id)) { c.padre = null; break; }
        visti.add(p.id);
        p = p.padre ? this.concetti.get(p.padre) : undefined;
      }
    }
    for (const c of this.concetti.values()) {
      if (!c.padre) continue;
      if (!this.figli.has(c.padre)) this.figli.set(c.padre, []);
      this.figli.get(c.padre)!.push(c);
    }
    for (const n of d.note) {
      this.note.set(n.id, n);
      const cs = n.concetti.filter((c) => this.concetti.has(c));
      for (const c of cs) {
        if (!this.notePerConcetto.has(c)) this.notePerConcetto.set(c, []);
        this.notePerConcetto.get(c)!.push(n);
        for (const o of cs) {
          if (o === c) continue;
          if (!this.insieme.has(c)) this.insieme.set(c, new Map());
          const m = this.insieme.get(c)!;
          m.set(o, (m.get(o) ?? 0) + 1);
        }
      }
    }
    for (const a of d.azioni) {
      if (!this.note.has(a.nota)) continue;
      this.azioni.set(a.id, a);
      if (!this.azioniPerNota.has(a.nota)) this.azioniPerNota.set(a.nota, []);
      this.azioniPerNota.get(a.nota)!.push(a);
    }
  }

  /** Il tema in cima all'albero che contiene questo (lui stesso, se sta in cima). */
  radiceDi(c: Concetto): Concetto {
    let x = c;
    while (x.padre) {
      const p = this.concetti.get(x.padre);
      if (!p) break;
      x = p;
    }
    return x;
  }

  /** I temi in cima all'albero, dai più presenti. */
  radici(): Concetto[] {
    return [...this.concetti.values()].filter((c) => !c.padre)
      .sort((a, b) => this.noteTutte(b.id).length - this.noteTutte(a.id).length || a.nome.localeCompare(b.nome, "it"));
  }

  /** Il tema e tutti i temi che stanno sopra di lui, fuorché quelli dentro `escluso` (per spostare). */
  destinazioni(escluso: string): Concetto[] {
    const fuori = new Set(this.discendenti(escluso).map((x) => x.id));
    const out: Concetto[] = [];
    const giu = (c: Concetto) => {
      if (fuori.has(c.id)) return;
      out.push(c);
      for (const f of this.sottoTemi(c.id)) giu(f);
    };
    for (const r of this.radici()) giu(r);
    return out;
  }

  quanteNote(c: string): number {
    return this.notePerConcetto.get(c)?.length ?? 0;
  }

  /** Data dell'ultima nota su un argomento (le note arrivano dalla più recente). */
  ultima(c: string): string | null {
    return this.noteTutte(c)[0]?.data ?? null;
  }

  /** Il tema e tutti i suoi sotto-temi. */
  discendenti(c: string): Concetto[] {
    const out: Concetto[] = [];
    const giu = (id: string) => {
      const x = this.concetti.get(id);
      if (!x) return;
      out.push(x);
      for (const f of this.figli.get(id) ?? []) giu(f.id);
    };
    giu(c);
    return out;
  }

  /** Gli antenati di un tema, dalla radice (escluso il tema). */
  percorso(c: string): Concetto[] {
    const out: Concetto[] = [];
    let p = this.concetti.get(c)?.padre;
    while (p) {
      const x = this.concetti.get(p);
      if (!x) break;
      out.unshift(x);
      p = x.padre;
    }
    return out;
  }

  /** Note del tema e dei suoi sotto-temi. */
  noteTutte(c: string): NotaG[] {
    const viste = new Map<string, NotaG>();
    for (const x of this.discendenti(c)) for (const n of this.notePerConcetto.get(x.id) ?? []) viste.set(n.id, n);
    return [...viste.values()].sort((a, b) => (b.data ?? "").localeCompare(a.data ?? ""));
  }

  /** Sotto-temi diretti, dai più recenti. */
  sottoTemi(c: string): Concetto[] {
    return [...(this.figli.get(c) ?? [])].sort((a, b) =>
      (this.noteTutte(b.id)[0]?.data ?? "").localeCompare(this.noteTutte(a.id)[0]?.data ?? ""));
  }

  vicini(c: string): { concetto: Concetto; peso: number }[] {
    return [...(this.insieme.get(c) ?? new Map<string, number>())]
      .map(([id, peso]) => ({ concetto: this.concetti.get(id)!, peso }))
      .filter((v) => v.concetto)
      .sort((a, b) => b.peso - a.peso || a.concetto.nome.localeCompare(b.concetto.nome, "it"));
  }

  /** Cose da fare ancora aperte di un gruppo di note (tutte, se non indicate), dalle più recenti. */
  aperte(note?: NotaG[]): AzioneG[] {
    const out: AzioneG[] = [];
    for (const n of note ?? this.note.values()) {
      for (const a of this.azioniPerNota.get(n.id) ?? []) if (a.stato !== "annullata") out.push(a);
    }
    return out;
  }

  /** Il tema di una cosa da fare (se è ancora sulla mappa). */
  temaDi(a: AzioneG): Concetto | null {
    return (a.tema && this.concetti.get(a.tema)) || null;
  }

  /** Cose da fare raggruppate per tema, i temi con più cose aperte e più recenti prima. */
  perTema(azioni: AzioneG[]): { tema: Concetto | null; azioni: AzioneG[] }[] {
    const gruppi = new Map<string, { tema: Concetto | null; azioni: AzioneG[] }>();
    for (const a of azioni) {
      const t = this.temaDi(a);
      const k = t?.id ?? "";
      if (!gruppi.has(k)) gruppi.set(k, { tema: t, azioni: [] });
      gruppi.get(k)!.azioni.push(a);
    }
    return [...gruppi.values()].sort((x, y) =>
      (x.tema ? 0 : 1) - (y.tema ? 0 : 1) || y.azioni.length - x.azioni.length ||
      (this.ultima(y.tema?.id ?? "") ?? "").localeCompare(this.ultima(x.tema?.id ?? "") ?? ""));
  }

  /** Temi che si sono mossi di recente, dal più recente: quelli con una scheda. */
  inMovimento(giorni = 21, max = 6): Concetto[] {
    const da = Date.now() - giorni * 86400_000;
    return [...this.concetti.values()]
      .filter((c) => c.sintesi && new Date(this.ultima(c.id) ?? 0).getTime() >= da)
      .sort((a, b) => (this.ultima(b.id) ?? "").localeCompare(this.ultima(a.id) ?? ""))
      .slice(0, max);
  }

  /** Cose da fare aperte di un tema e dei suoi sotto-temi. */
  apertePerTema(c: string): number {
    const ids = new Set(this.discendenti(c).map((x) => x.id));
    let n = 0;
    for (const a of this.azioni.values()) if (a.tema && ids.has(a.tema) && a.stato === "aperta") n++;
    return n;
  }
}

export const NOME_TIPO: Record<TipoTema, string> = { progetto: "progetto", idea: "idea", tema: "tema" };

/** La prima frase di un testo, per le righe brevi. */
export function primaFrase(s: string | null | undefined): string {
  if (!s) return "";
  const m = s.match(/^.*?[.!?…](?=\s|$)/);
  return (m ? m[0] : s).trim();
}
