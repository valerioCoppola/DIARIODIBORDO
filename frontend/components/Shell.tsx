"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { loadSession, type Me } from "@/lib/api";
import {
  EVENTO, chiudiDiario, diarioInCache, motivoPerNonChiudere, ricordaDiario, vestiCopertina, type DiarioCorrente,
} from "@/lib/diario";

const giorno = new Intl.DateTimeFormat("it-IT", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

/** Verifica la sessione e porta dove serve: all'accesso senza sessione, al cambio della
 *  password provvisoria, allo scaffale se la pagina vuole un diario e non ce n'è uno aperto. */
export function useSession(opts: { diario?: boolean; account?: boolean } = {}): { me: Me | null; ready: boolean } {
  const vuoleDiario = opts.diario ?? true;
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    loadSession()
      .then((m) => {
        if (!m) return window.location.replace("/accesso/");
        if (m.cambia_password && !opts.account) return window.location.replace("/account/");
        if (vuoleDiario && !m.diario) {
          ricordaDiario(null);
          return window.location.replace("/diari/");
        }
        if (vuoleDiario) ricordaDiario(m.diario);
        setMe(m);
      })
      .catch(() => setMe(null))
      .finally(() => setReady(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return { me, ready };
}

/** Il nome del diario aperto: subito dal ricordo locale, poi confermato dal PC. */
function useDiarioCorrente(): DiarioCorrente | null {
  const [d, setD] = useState<DiarioCorrente | null>(null);
  useLayoutEffect(() => {
    const c = diarioInCache();
    setD(c);
    vestiCopertina(c?.copertina ?? null);
    const h = (e: Event) => setD((e as CustomEvent<DiarioCorrente | null>).detail);
    window.addEventListener(EVENTO, h);
    return () => window.removeEventListener(EVENTO, h);
  }, []);
  return d;
}

const SOGLIA = 56;          // px: "di poco" — il nastro tirato giù di tanto chiude il diario
const riduciMovimento = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Il nastro segnalibro in cima alla pagina: tirato giù chiude il diario a questa pagina. */
function Nastro({ diario }: { diario: DiarioCorrente | null }) {
  const [tiro, setTiro] = useState(0);
  const [trascina, setTrascina] = useState(false);
  const [avviso, setAvviso] = useState<string | null>(null);
  const [chiude, setChiude] = useState<DOMRect | null>(null);
  const start = useRef<{ y: number; id: number } | null>(null);
  const oltre = useRef(false);
  const btn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!avviso) return;
    const t = setTimeout(() => setAvviso(null), 3200);
    return () => clearTimeout(t);
  }, [avviso]);

  async function chiudi() {
    const no = motivoPerNonChiudere();
    if (no) {
      setAvviso(no);
      setTiro(0);
      return;
    }
    const pagina = btn.current?.closest(".wrap")?.getBoundingClientRect() ?? null;
    if (!riduciMovimento() && pagina) setChiude(pagina);
    const animazione = new Promise((r) => setTimeout(r, riduciMovimento() ? 0 : 620));
    try {
      await chiudiDiario();
    } catch {
      /* il PC non risponde: lo scaffale lo dirà; il diario resta segnato a questa pagina */
    }
    await animazione;
    window.location.assign("/diari/");
  }

  function down(e: React.PointerEvent) {
    start.current = { y: e.clientY, id: e.pointerId };
    oltre.current = false;
    setTrascina(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function move(e: React.PointerEvent) {
    if (!start.current || start.current.id !== e.pointerId) return;
    const dy = Math.max(0, e.clientY - start.current.y);
    // Oltre la soglia il nastro si allunga sempre meno: si sente che "basta così".
    const t = dy <= SOGLIA ? dy : SOGLIA + (dy - SOGLIA) * 0.3;
    setTiro(Math.min(t, SOGLIA + 40));
    const ora = dy >= SOGLIA;
    if (ora && !oltre.current) navigator.vibrate?.(8);
    oltre.current = ora;
  }
  function up(e: React.PointerEvent) {
    if (!start.current || start.current.id !== e.pointerId) return;
    const dy = e.clientY - start.current.y;
    start.current = null;
    setTrascina(false);
    if (oltre.current) {
      void chiudi();
    } else {
      setTiro(0);
      if (Math.abs(dy) < 6) setAvviso("Tira giù il nastro per chiudere il diario");
    }
  }

  const pronto = tiro >= SOGLIA;
  return (
    <>
      <button ref={btn} type="button" className="nastro" data-trascina={trascina || undefined}
        aria-label="Chiudi il diario a questa pagina (tira giù il nastro)"
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); void chiudi(); } }}
        onClick={(e) => e.preventDefault()}>
        <span className="nastro-tela" style={{ height: `calc(5.5rem + ${tiro}px)` }} aria-hidden="true" />
      </button>
      {(tiro > 10 || avviso) && (
        <span className="nastro-dice" data-pronto={pronto || undefined} role="status"
          style={{ top: `calc(${tiro}px + 3.5rem)` }}>
          {avviso ?? (pronto ? "Lascia: chiudo il diario" : "Chiudi il diario")}
        </span>
      )}
      {chiude && (
        <div className="si-chiude" aria-hidden="true"
          style={{ left: chiude.left, top: chiude.top, width: chiude.width, height: chiude.height }}>
          <span className="si-chiude-nome">{diario?.nome ?? "Diario di Bordo"}</span>
        </div>
      )}
    </>
  );
}

export function Shell({ active, children }: { active: "registra" | "archivio" | "mappa" | "cerca"; children?: React.ReactNode }) {
  const [oggi, setOggi] = useState("");
  const diario = useDiarioCorrente();
  useEffect(() => setOggi(giorno.format(new Date())), []);
  return (
    <>
      <header className="copertina">
        <h1><a href="/">{diario?.nome ?? "\u00a0"}</a></h1>
      </header>
      <main className="wrap">
        <Nastro diario={diario} />
        <p className="oggi">{oggi || "\u00a0"}</p>
        {children != null && <div className="corpo">{children}</div>}
      </main>
      <nav className="nav" aria-label="Sezioni">
        <a href="/" aria-current={active === "registra" ? "page" : undefined}>Registra</a>
        <a href="/archivio/" aria-current={active === "archivio" ? "page" : undefined}>Archivio</a>
        <a href="/mappa/" aria-current={active === "mappa" ? "page" : undefined}>Mappa</a>
        <a href="/cerca/" aria-current={active === "cerca" ? "page" : undefined}>Cerca</a>
      </nav>
    </>
  );
}

/** Le pagine fuori dai diari (scaffale, account, persone): stesso cuoio, niente nastro né sezioni. */
export function Fuori({ titolo, children }: { titolo: string; children?: React.ReactNode }) {
  useLayoutEffect(() => vestiCopertina("scaffale"), []);
  return (
    <>
      <header className="copertina">
        <h1><a href="/diari/">{titolo}</a></h1>
      </header>
      <main className="fuori">{children}</main>
    </>
  );
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

export function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} kB`;
  return `${(n / (1024 * 1024)).toFixed(1).replace(".", ",")} MB`;
}

const quando = new Intl.DateTimeFormat("it-IT", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
export function formatWhen(iso: string | null | undefined): string {
  return iso ? quando.format(new Date(iso)) : "Data non disponibile";
}
