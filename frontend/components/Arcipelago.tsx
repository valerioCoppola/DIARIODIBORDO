"use client";

import { hierarchy, pack, type HierarchyCircularNode } from "d3-hierarchy";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { type Concetto, type Grafo } from "@/lib/grafo";

/** I temi più ampi come isole, le loro parti come bolle dentro, e così via in profondità:
 *  più note, bolla più grande. Il disegno per cerchi annidati non sovrappone mai niente.
 *  Toccando un'isola o un tema con sotto-temi lo si avvicina; i nomi vanno a capo dentro
 *  la bolla, e una bolla con sotto-temi porta il nome lungo il bordo.
 *
 *  Colori: ogni tema ha la sua tinta (calcolata dal PC sulla forma dell'albero: le isole
 *  distanziate, le sorelle che si dividono la fascia del tema che le contiene). La mappa ci
 *  mette la luminosità: più in profondità, più piena; tra sorelle vicine, un'alternanza di
 *  chiaro e scuro, così due bolle accanto si distinguono anche quando le tinte sono vicine.
 *  Un tema provvisorio (nato da una collocazione incerta) ha il bordo tratteggiato. */

type Dato = { concetto?: Concetto; proprio?: boolean; n?: number; children?: Dato[] };
type Nodo = HierarchyCircularNode<Dato>;
type Vista = [number, number, number]; // centro x, centro y, diametro visibile

const LATO = 1000;
const BORDO_AREA = 52;        // spazio dentro l'isola per il nome scritto lungo la costa
const BORDO_TEMA = 0.22;      // in una bolla con sotto-temi, lo spazio per il nome (frazione del raggio)
const DURATA = 480;

let ctx: CanvasRenderingContext2D | null | undefined;
function larghezza(testo: string, px: number): number {
  if (ctx === undefined) {
    try { ctx = document.createElement("canvas").getContext("2d"); } catch { ctx = null; }
  }
  if (!ctx) return testo.length * px * 0.5;
  ctx.font = `italic 600 ${px}px "Cormorant Garamond", Georgia, serif`;
  return ctx.measureText(testo).width;
}

/** Il testo più lungo che sta in `max` pixel, con i puntini se va accorciato. */
function adatta(testo: string, px: number, max: number): string | null {
  if (larghezza(testo, px) <= max) return testo;
  for (let n = testo.length - 1; n >= 3; n--) {
    const t = `${testo.slice(0, n).trimEnd()}…`;
    if (larghezza(t, px) <= max) return t;
  }
  return null;
}

/** Il nome a capo dentro un cerchio di raggio r (pixel): righe e corpo del carattere, o null se non ci sta. */
function aCapo(testo: string, r: number): { righe: string[]; px: number } | null {
  const parole = testo.split(/\s+/).filter(Boolean);
  for (let px = Math.min(17, Math.max(11, r * 0.36)); px >= 9.5; px -= 0.5) {
    const maxRighe = Math.max(1, Math.min(3, Math.floor((r * 1.45) / (px * 1.12))));
    const righe: string[] = [];
    let ok = true;
    let cur = "";
    for (const p of parole) {
      const prova = cur ? `${cur} ${p}` : p;
      // La corda disponibile si accorcia allontanandosi dal centro.
      const y = (righe.length - (maxRighe - 1) / 2) * px * 1.12;
      const corda = 2 * Math.sqrt(Math.max(0, r * r * 0.86 - y * y));
      if (larghezza(prova, px) <= corda) { cur = prova; continue; }
      if (!cur) { ok = false; break; }
      righe.push(cur);
      cur = p;
      if (righe.length >= maxRighe) { ok = false; break; }
    }
    if (ok && cur) righe.push(cur);
    if (ok && righe.length <= maxRighe && righe.every((x) => larghezza(x, px) <= r * 1.9)) return { righe, px };
  }
  const una = adatta(testo, 9.5, r * 1.7);
  return una ? { righe: [una], px: 9.5 } : null;
}

export function Arcipelago({ grafo, fuoco, onConcetto, onSu }: {
  grafo: Grafo;
  fuoco: string | null;
  onConcetto: (id: string) => void;
  onSu: () => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [px, setPx] = useState(340);
  const [vista, setVista] = useState<Vista | null>(null);
  const [inMoto, setInMoto] = useState(false);
  const vistaRef = useRef(vista);
  vistaRef.current = vista;

  const radice = useMemo(() => {
    const tema = (c: Concetto): Dato => {
      const figli = grafo.sottoTemi(c.id);
      const n = grafo.quanteNote(c.id);
      if (figli.length === 0) return { concetto: c, n: Math.max(1, n) };
      // Le note del tema stesso occupano una bolla invisibile accanto ai sotto-temi.
      return { concetto: c, children: [...figli.map(tema), ...(n > 0 ? [{ proprio: true, n: n * 0.5 }] : [])] };
    };
    const dati: Dato = { children: grafo.radici().map(tema) };
    const h = hierarchy<Dato>(dati).sum((d) => (d.children ? 0 : Math.max(d.proprio ? 0.5 : 1, d.n ?? 1)))
      .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
    const r = pack<Dato>().size([LATO, LATO])
      .padding((d) => (d.depth === 0 ? 18 : d.depth === 1 && d.children ? BORDO_AREA : d.children ? 6 : 4))(h);
    // In una bolla con sotto-temi i figli si stringono verso il centro: resta il bordo per il nome.
    // (each va in ampiezza: un tema si stringe prima dei suoi sotto-temi.)
    r.each((n) => {
      if (n.depth < 2 || !n.children || !n.data.concetto) return;
      const f = 1 - BORDO_TEMA;
      n.each((d) => {
        if (d === n) return;
        d.x = n.x + (d.x - n.x) * f;
        d.y = n.y + (d.y - n.y) * f;
        d.r = d.r * f;
      });
    });
    return r;
  }, [grafo]);

  const nodi = useMemo(() => radice.descendants() as Nodo[], [radice]);

  // Alternanza chiaro/scuro tra sorelle, nell'ordine delle tinte: due tinte vicine hanno luminosità diverse.
  const alterna = useMemo(() => {
    const m = new Map<string, boolean>();
    for (const n of nodi) {
      const sorelle = ((n.children ?? []) as Nodo[]).filter((x) => x.data.concetto)
        .sort((a, b) => a.data.concetto!.tinta - b.data.concetto!.tinta);
      sorelle.forEach((x, i) => m.set(x.data.concetto!.id, i % 2 === 1));
    }
    return m;
  }, [nodi]);

  const isole = (radice.children ?? []) as Nodo[];
  const perId = useMemo(() => {
    const m = new Map<string, Nodo>();
    for (const n of nodi) if (n.data.concetto) m.set(n.data.concetto.id, n);
    return m;
  }, [nodi]);

  // Il nodo a fuoco: si avvicina il tema se ha sotto-temi, altrimenti il suo contenitore.
  const nodoFuoco = fuoco ? perId.get(fuoco) : undefined;
  const contenitore = nodoFuoco ? (nodoFuoco.children ? nodoFuoco : (nodoFuoco.parent as Nodo | null)) : null;

  const [tutto, forma] = useMemo((): [Vista, number] => {
    if (isole.length === 0) return [[LATO / 2, LATO / 2, LATO], 1];
    const x0 = Math.min(...isole.map((n) => n.x - n.r)), x1 = Math.max(...isole.map((n) => n.x + n.r));
    const y0 = Math.min(...isole.map((n) => n.y - n.r)), y1 = Math.max(...isole.map((n) => n.y + n.r));
    const f = Math.min(4 / 3, Math.max(3 / 4, (x1 - x0) / (y1 - y0)));
    const d = Math.max(Math.min(x1 - x0, y1 - y0), f >= 1 ? (x1 - x0) / f : (y1 - y0) * f) * 1.03;
    return [[(x0 + x1) / 2, (y0 + y1) / 2, d], f];
  }, [isole]);
  const meta = useMemo((): Vista => {
    const n = contenitore && contenitore.depth > 0 ? contenitore : null;
    return n ? [n.x, n.y, n.r * 2 * 1.04] : tutto;
  }, [contenitore, tutto]);

  useLayoutEffect(() => {
    const el = svg.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setPx(el.clientWidth || 340));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const da = vistaRef.current;
    if (da === null) {
      setVista(meta);
      return;
    }
    if (da[0] === meta[0] && da[1] === meta[1] && da[2] === meta[2]) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setVista(meta);
      return;
    }
    let raf = 0;
    const t0 = performance.now();
    setInMoto(true);
    const passo = (t: number) => {
      const u = Math.min(1, (t - t0) / DURATA);
      const e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
      setVista([da[0] + (meta[0] - da[0]) * e, da[1] + (meta[1] - da[1]) * e, da[2] * Math.pow(meta[2] / da[2], e)]);
      if (u < 1) raf = requestAnimationFrame(passo);
      else setInMoto(false);
    };
    raf = requestAnimationFrame(passo);
    return () => cancelAnimationFrame(raf);
  }, [meta]);

  const [cx, cy, d] = vista ?? meta;
  const w = forma >= 1 ? d * forma : d, h = forma >= 1 ? d : d / forma;
  const k = px / w;
  const u = (p: number) => p / k;

  /** Vero se n sta dentro il contenitore a fuoco (o se non c'è fuoco). */
  const dentro = (n: Nodo) => {
    if (!contenitore || contenitore.depth === 0) return true;
    for (let x: Nodo | null = n; x; x = x.parent as Nodo | null) if (x === contenitore) return true;
    return false;
  };
  const panoramica = !contenitore || contenitore.depth === 0;

  /** Il nome dentro una bolla senza sotto-temi: a capo, centrato. */
  const nomeDentro = (t: Nodo, nome: string) => {
    const l = aCapo(nome, t.r * k);
    if (!l) return null;
    const lh = l.px * 1.12;
    return (
      <text x={t.x} textAnchor="middle" fontSize={u(l.px)}>
        {l.righe.map((r, i) => (
          <tspan key={i} x={t.x} y={t.y + u((i - (l.righe.length - 1) / 2) * lh + l.px * 0.33)}>{r}</tspan>
        ))}
      </text>
    );
  };

  const bolla = (t: Nodo, isola: Nodo): React.ReactNode => {
    if (t.data.proprio) return null;
    const c = t.data.concetto!;
    const rpx = t.r * k;
    const vicino = dentro(t);
    // Si tocca un tema quando la sua isola è avvicinata; in panoramica si avvicina l'isola.
    const attivo = !panoramica && vicino;
    const scelto = fuoco === c.id;
    const figli = (t.children as Nodo[] | undefined) ?? [];
    let etichetta: React.ReactNode = null;
    if (!inMoto && vicino && rpx >= 13 && (t.depth - (contenitore?.depth ?? 1)) <= 2) {
      if (figli.length > 0) {
        const fs = Math.min(19, Math.max(10, rpx * BORDO_TEMA * 0.6));
        const rn = t.r - u(fs) * 0.95;
        const nome = adatta(c.nome, fs, Math.PI * rn * k * 0.75);
        if (nome && rn > 0) {
          const idArco = `arco-${c.id}`;
          etichetta = (
            <>
              <path id={idArco} fill="none" stroke="none"
                d={`M ${t.x - rn} ${t.y} A ${rn} ${rn} 0 0 1 ${t.x + rn} ${t.y}`} />
              <text className="nome-bolla" fontSize={u(fs)}>
                <textPath href={`#${idArco}`} startOffset="50%" textAnchor="middle">{nome}</textPath>
              </text>
            </>
          );
        }
      } else {
        etichetta = nomeDentro(t, c.nome);
      }
    }
    const radiceId = isola.data.concetto!.id;
    return (
      <g key={c.id} className="tema" data-liv={Math.min(4, t.depth - 1)} data-alt={alterna.get(c.id) || undefined}
        data-scelto={scelto || undefined} data-provvisorio={c.provvisorio || undefined}
        style={{ "--h": c.tinta } as React.CSSProperties}
        tabIndex={attivo ? 0 : -1} role={attivo ? "link" : undefined}
        aria-label={attivo ? c.nome + (c.provvisorio ? " (da confermare)" : "") : undefined}
        onClick={(ev) => {
          ev.stopPropagation();
          onConcetto(attivo ? c.id : radiceId);
        }}
        onKeyDown={(ev) => { if (ev.key === "Enter" && attivo) onConcetto(c.id); }}>
        <title>{c.nome}{c.provvisorio ? " — da confermare" : ""}</title>
        <circle cx={t.x} cy={t.y} r={t.r} />
        {figli.map((f) => bolla(f, isola))}
        {etichetta}
      </g>
    );
  };

  return (
    <svg ref={svg} className="arcipelago" viewBox={`${cx - w / 2} ${cy - h / 2} ${w} ${h}`}
      style={{ aspectRatio: `${forma}` }} data-vicino={fuoco && contenitore && contenitore.depth > 0 ? "" : undefined}
      role="group" aria-label="Mappa dei temi" onClick={() => { if (!panoramica) onSu(); }}>
      {isole.map((isola) => {
        const c = isola.data.concetto!;
        const conFigli = !!isola.children?.some((x) => (x as Nodo).data.concetto);
        let antenata = false;
        for (let x = contenitore; x; x = x.parent as Nodo | null) if (x === isola) antenata = true;
        const sfuma = !panoramica && !dentro(isola) && !antenata;
        const fsNome = Math.min(u(13), BORDO_AREA * 0.6);
        const rNome = isola.r - Math.min(BORDO_AREA * 0.18, u(5)) - fsNome * 0.72;
        const nome = adatta(c.nome.toUpperCase(), fsNome * k, Math.PI * rNome * k * 0.8);
        const idArco = `arco-i-${c.id}`;
        const inIsola = contenitore && contenitore !== isola && dentro(contenitore);
        return (
          <g key={c.id} className="isola" data-sfuma={sfuma || undefined} data-scelto={fuoco === c.id || undefined}
            data-provvisorio={c.provvisorio || undefined} style={{ "--h": c.tinta } as React.CSSProperties}
            onClick={(ev) => { ev.stopPropagation(); if (panoramica || sfuma) onConcetto(c.id); else onSu(); }}
            tabIndex={panoramica ? 0 : -1} role="button" aria-label={c.nome}
            onKeyDown={(ev) => { if (ev.key === "Enter") onConcetto(c.id); }}>
            <title>{c.nome}{c.provvisorio ? " — da confermare" : ""}</title>
            <circle className="costa" cx={isola.x} cy={isola.y} r={isola.r} />
            {conFigli ? (
              <>
                <path id={idArco} fill="none" stroke="none"
                  d={`M ${isola.x - rNome} ${isola.y} A ${rNome} ${rNome} 0 0 1 ${isola.x + rNome} ${isola.y}`} />
                {nome && fsNome * k >= 8 && !sfuma && !inIsola && (
                  <text className="nome-isola" fontSize={fsNome} letterSpacing={fsNome * 0.12}>
                    <textPath href={`#${idArco}`} startOffset="50%" textAnchor="middle">{nome}</textPath>
                  </text>
                )}
              </>
            ) : (!sfuma && !inMoto && <g className="nome-isola-dentro">{nomeDentro(isola, c.nome)}</g>)}
            {(isola.children as Nodo[] | undefined)?.map((t) => bolla(t, isola))}
          </g>
        );
      })}
    </svg>
  );
}
