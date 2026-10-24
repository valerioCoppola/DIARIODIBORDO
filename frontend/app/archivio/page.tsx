"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Calendario } from "@/components/Calendario";
import { Shell, useSession } from "@/components/Shell";
import { VoceCard } from "@/components/VoceCard";
import { api, logout } from "@/lib/api";
import { chiudiDiario, motivoPerNonChiudere } from "@/lib/diario";
import {
  daIso, etichettaGiorno, fuso, isoGiorno, isoMese, perGiorno,
  type Calendario as DatiCalendario, type Voce,
} from "@/lib/archivio";
import { CONCLUSI } from "@/lib/fasi";

type Vista = "recenti" | "calendario";
type Elenco = { totale: number; elementi: Voce[] };
type Status = { in_elaborazione: number; elaboratore: { attivo: boolean } };

const PAGINA = 30;
const MEMO = "ddb-archivio";

function leggiMemo(): { vista?: Vista; giorno?: string } {
  try {
    return JSON.parse(localStorage.getItem(MEMO) ?? "{}");
  } catch {
    return {};
  }
}

function scriviMemo(v: { vista: Vista; giorno: string }) {
  try {
    localStorage.setItem(MEMO, JSON.stringify(v));
  } catch { /* archiviazione del browser non disponibile: pazienza */ }
}

/** Come tirare giù il nastro, per chi non lo trova: il segno resta all'archivio. */
function chiudi() {
  if (motivoPerNonChiudere()) return;
  void chiudiDiario().catch(() => {}).finally(() => window.location.assign("/diari/"));
}

export default function ArchivioPage() {
  const { me } = useSession();
  const tz = useMemo(() => fuso(), []);
  const [vista, setVista] = useState<Vista>("recenti");
  const [giorno, setGiorno] = useState(() => isoGiorno(new Date()));
  const [mese, setMese] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  const [recenti, setRecenti] = useState<Voce[] | null>(null);
  const [totale, setTotale] = useState(0);
  const [delGiorno, setDelGiorno] = useState<Voce[] | null>(null);
  const [cal, setCal] = useState<DatiCalendario | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Vista e giorno: dall'indirizzo, altrimenti dall'ultima visita.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const memo = leggiMemo();
    const v = (p.get("vista") ?? memo.vista) === "calendario" ? "calendario" : "recenti";
    const g = p.get("giorno") ?? (v === "calendario" ? memo.giorno : null);
    setVista(v);
    if (g && /^\d{4}-\d{2}-\d{2}$/.test(g)) {
      setGiorno(g);
      const d = daIso(g);
      setMese(new Date(d.getFullYear(), d.getMonth(), 1));
    }
  }, []);

  useEffect(() => {
    scriviMemo({ vista, giorno });
    const url = new URL(window.location.href);
    url.searchParams.set("vista", vista);
    if (vista === "calendario") url.searchParams.set("giorno", giorno); else url.searchParams.delete("giorno");
    window.history.replaceState(null, "", url);
  }, [vista, giorno]);

  const caricaRecenti = useCallback(async (offset = 0) => {
    const r = await api<Elenco>(`/api/v1/recordings?limit=${PAGINA}&offset=${offset}&tz=${encodeURIComponent(tz)}`);
    setTotale(r.totale);
    setRecenti((prev) => (offset === 0 || !prev ? r.elementi : [...prev, ...r.elementi]));
  }, [tz]);

  const caricaGiorno = useCallback(async (g: string) => {
    const r = await api<Elenco>(`/api/v1/recordings?limit=200&giorno=${g}&tz=${encodeURIComponent(tz)}`);
    setDelGiorno(r.elementi);
  }, [tz]);

  const caricaMese = useCallback(async (m: Date) => {
    setCal(await api<DatiCalendario>(`/api/v1/calendario?mese=${isoMese(m)}&tz=${encodeURIComponent(tz)}`));
  }, [tz]);

  const aggiorna = useCallback(async () => {
    try {
      const jobs: Promise<unknown>[] = [api<Status>("/api/v1/status").then(setStatus)];
      if (vista === "recenti") jobs.push(caricaRecenti(0));
      else jobs.push(caricaGiorno(giorno), caricaMese(mese));
      await Promise.all(jobs);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [vista, giorno, mese, caricaRecenti, caricaGiorno, caricaMese]);

  useEffect(() => {
    if (me) void aggiorna();
  }, [me, aggiorna]);

  // Finché qualcosa è in elaborazione, l'elenco si aggiorna da solo.
  const visibili = vista === "recenti" ? recenti : delGiorno;
  const inCorso = (visibili ?? []).some((v) => v.job && !CONCLUSI.has(v.job.stato));
  useEffect(() => {
    if (!inCorso) return;
    const t = setInterval(() => void aggiorna(), 5000);
    return () => clearInterval(t);
  }, [inCorso, aggiorna]);

  function cambiaMese(delta: number) {
    setMese((m) => new Date(m.getFullYear(), m.getMonth() + delta, 1));
  }

  function vaiAOggi() {
    const d = new Date();
    setMese(new Date(d.getFullYear(), d.getMonth(), 1));
    setGiorno(isoGiorno(d));
  }

  if (!me) return <Shell active="archivio" />;

  const gruppi = recenti ? perGiorno(recenti) : [];

  return (
    <Shell active="archivio">
      <div className="viste" role="tablist" aria-label="Vista dell'archivio">
        <button type="button" role="tab" aria-selected={vista === "recenti"} onClick={() => setVista("recenti")}>
          Più recenti
        </button>
        <button type="button" role="tab" aria-selected={vista === "calendario"} onClick={() => setVista("calendario")}>
          Calendario
        </button>
      </div>

      {error && <p className="avviso grave" role="alert">{error}</p>}
      {status && status.in_elaborazione > 0 && !status.elaboratore.attivo && (
        <p className="avviso" role="status">
          Elaborazione ferma sul PC: i vocali aspettano in coda.
        </p>
      )}

      {vista === "recenti" && (
        <section aria-label="Vocali dal più recente">
          {recenti === null ? (
            <p className="vuoto">Caricamento…</p>
          ) : recenti.length === 0 ? (
            <p className="vuoto">Ancora nessun vocale.</p>
          ) : (
            <>
              {gruppi.map((g) => (
                <div key={g.giorno} className="gruppo-giorno">
                  <h2 className="giorno-testa">{etichettaGiorno(g.giorno)}</h2>
                  <ul className="voci-card">{g.voci.map((v) => <VoceCard key={v.id} v={v} />)}</ul>
                </div>
              ))}
              {recenti.length < totale && (
                <p className="altri">
                  <button className="piccolo" type="button" onClick={() => void caricaRecenti(recenti.length)}>
                    Mostra altri ({totale - recenti.length})
                  </button>
                </p>
              )}
            </>
          )}
        </section>
      )}

      {vista === "calendario" && (
        <>
          <Calendario mese={mese} dati={cal} selezionato={giorno} onMese={cambiaMese}
            onGiorno={(g) => setGiorno(g)} />
          <p className="cal-oggi">
            {giorno !== isoGiorno(new Date()) && (
              <button className="piccolo" type="button" onClick={vaiAOggi}>Torna a oggi</button>
            )}
          </p>
          <section className="gruppo-giorno" aria-live="polite">
            <h2 className="giorno-testa">{etichettaGiorno(giorno)}</h2>
            {delGiorno === null ? (
              <p className="vuoto">Caricamento…</p>
            ) : delGiorno.length === 0 ? (
              <p className="vuoto">Nessun vocale in questo giorno.</p>
            ) : (
              <ul className="voci-card">{delGiorno.map((v) => <VoceCard key={v.id} v={v} />)}</ul>
            )}
          </section>
        </>
      )}

      <p className="esci">
        <button className="piccolo" type="button" onClick={chiudi}>Chiudi il diario</button>
        <button className="piccolo" type="button"
          onClick={() => logout().finally(() => window.location.replace("/accesso/"))}>
          Esci da {me.username}
        </button>
      </p>
    </Shell>
  );
}
