"use client";

import { useCallback, useEffect, useState } from "react";
import { HoldButton } from "@/components/HoldButton";
import { Fuori, useSession } from "@/components/Shell";
import { ApiError, api } from "@/lib/api";

type Utente = {
  id: string; username: string; amministratore: boolean; attivo: boolean; password_provvisoria: boolean;
  creato_il: string; ultimo_accesso: string | null; visto_il: string | null; sei_tu: boolean;
};
type Invito = {
  id: string; nota: string | null; creato_il: string; scade_il: string; usato_il: string | null;
  usato_da: string | null; stato: "valido" | "usato" | "scaduto" | "revocato";
};

const data = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short", year: "numeric" });
const dataOra = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Un testo da passare a voce, per messaggio o copiato: codice o password provvisoria. */
function DaConsegnare({ titolo, valore, messaggio, onChiudi }: {
  titolo: string; valore: string; messaggio: string; onChiudi: () => void;
}) {
  const [copiato, setCopiato] = useState(false);
  const puoCondividere = typeof navigator !== "undefined" && !!navigator.share;
  async function copia() {
    try {
      await navigator.clipboard.writeText(messaggio);
      setCopiato(true);
    } catch { /* senza permesso: resta da copiare a mano */ }
  }
  return (
    <div className="consegna" role="status">
      <p className="mc">{titolo}</p>
      <p className="consegna-valore">{valore}</p>
      <p className="consegna-msg">{messaggio}</p>
      <p className="consegna-comandi">
        {puoCondividere && (
          <button className="primario" type="button"
            onClick={() => navigator.share({ text: messaggio }).catch(() => {})}>Invia…</button>
        )}
        <button className="piccolo" type="button" onClick={copia}>{copiato ? "Copiato" : "Copia il messaggio"}</button>
        <button className="piccolo" type="button" onClick={onChiudi}>Fatto</button>
      </p>
      <p className="scheda-meta">Si vede solo ora: il PC ne conserva solo l’impronta.</p>
    </div>
  );
}

export default function PersonePage() {
  const { me } = useSession({ diario: false });
  const [utenti, setUtenti] = useState<Utente[] | null>(null);
  const [inviti, setInviti] = useState<Invito[]>([]);
  const [nota, setNota] = useState("");
  const [consegna, setConsegna] = useState<{ titolo: string; valore: string; messaggio: string } | null>(null);
  const [errore, setErrore] = useState<string | null>(null);

  const carica = useCallback(async () => {
    try {
      const [u, i] = await Promise.all([
        api<{ utenti: Utente[] }>("/api/v1/admin/utenti"),
        api<{ inviti: Invito[] }>("/api/v1/admin/inviti"),
      ]);
      setUtenti(u.utenti);
      setInviti(i.inviti);
    } catch (err) {
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde");
    }
  }, []);

  useEffect(() => {
    if (!me) return;
    if (!me.amministratore) return window.location.replace("/diari/");
    void carica();
  }, [me, carica]);

  async function fai(fn: () => Promise<unknown>) {
    setErrore(null);
    try {
      await fn();
      await carica();
    } catch (err) {
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde");
    }
  }

  async function nuovoInvito(e: React.FormEvent) {
    e.preventDefault();
    await fai(async () => {
      const r = await api<{ codice: string; scade_il: string }>("/api/v1/admin/inviti", {
        method: "POST", json: { nota: nota.trim() || null },
      });
      setNota("");
      setConsegna({
        titolo: "Codice d’invito",
        valore: r.codice,
        messaggio: `Ti ho invitato nel Diario di Bordo. Apri ${location.origin}/registrati/ ` +
          `(serve Tailscale), inserisci il codice ${r.codice} e scegli nome utente e password. ` +
          `Vale una volta, fino al ${data.format(new Date(r.scade_il))}.`,
      });
    });
  }

  async function provvisoria(u: Utente) {
    await fai(async () => {
      const r = await api<{ password: string }>(`/api/v1/admin/utenti/${u.id}/password`, { method: "POST" });
      setConsegna({
        titolo: `Password provvisoria per ${u.username}`,
        valore: r.password,
        messaggio: `La tua password provvisoria per il Diario di Bordo è ${r.password}. ` +
          "Al prossimo accesso ti chiederà di sceglierne una tua.",
      });
    });
  }

  if (!me?.amministratore) return <Fuori titolo="Persone" />;

  return (
    <Fuori titolo="Persone">
      <div className="foglio">
        <p className="fuori-intro">
          Qui gestisci gli account. I diari di ciascuno restano privati: da qui non si vedono.
        </p>
        {errore && <p className="avviso grave" role="alert">{errore}</p>}
        {consegna && <DaConsegnare {...consegna} onChiudi={() => setConsegna(null)} />}

        <section className="registro" aria-labelledby="t-inviti">
          <h2 id="t-inviti">Invitare qualcuno</h2>
          <p>Crea un codice e mandalo alla persona: con quello si crea da sola l’account e sceglie la password.</p>
          <form className="invito-nuovo" onSubmit={nuovoInvito}>
            <div className="campo">
              <label htmlFor="nota">Per chi è (facoltativo)</label>
              <input id="nota" value={nota} maxLength={80} placeholder="es. Marta" onChange={(e) => setNota(e.target.value)} />
            </div>
            <button className="primario" type="submit">Crea un invito</button>
          </form>
          {inviti.length > 0 && (
            <ul className="elenco-persone">
              {inviti.map((i) => (
                <li key={i.id}>
                  <span className="persona-nome">{i.nota || "Invito"}</span>
                  <span className="persona-meta">
                    {i.stato === "usato" ? `usato da ${i.usato_da}, ${data.format(new Date(i.usato_il!))}`
                      : i.stato === "valido" ? `valido fino al ${data.format(new Date(i.scade_il))}`
                      : i.stato}
                  </span>
                  {i.stato === "valido" && (
                    <button className="piccolo" type="button"
                      onClick={() => fai(() => api(`/api/v1/admin/inviti/${i.id}`, { method: "DELETE" }))}>
                      Ritira
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="registro" aria-labelledby="t-persone">
          <h2 id="t-persone">Account</h2>
          {utenti === null ? <p className="vuoto">Caricamento…</p> : (
            <ul className="elenco-persone">
              {utenti.map((u) => (
                <li key={u.id} data-disattivo={!u.attivo || undefined}>
                  <span className="persona-nome">
                    {u.username}{u.sei_tu ? " (tu)" : ""}
                  </span>
                  <span className="persona-meta">
                    {[u.amministratore ? "amministratore" : null, !u.attivo ? "disattivato" : null,
                      u.password_provvisoria ? "password provvisoria" : null,
                      (u.visto_il ?? u.ultimo_accesso) ? `visto ${dataOra.format(new Date((u.visto_il ?? u.ultimo_accesso)!))}`
                        : "mai entrato"].filter(Boolean).join(" · ")}
                  </span>
                  {!u.sei_tu && (
                    <span className="persona-azioni">
                      {u.attivo && (
                        <button className="piccolo" type="button" onClick={() => provvisoria(u)}>
                          Password provvisoria
                        </button>
                      )}
                      {u.attivo ? (
                        <HoldButton label="Disattiva" hint="tieni premuto 2 s"
                          onConfirm={() => fai(() => api(`/api/v1/admin/utenti/${u.id}/disattiva`, { method: "POST" }))} />
                      ) : (
                        <button className="piccolo" type="button"
                          onClick={() => fai(() => api(`/api/v1/admin/utenti/${u.id}/attiva`, { method: "POST" }))}>
                          Riattiva
                        </button>
                      )}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="scheda-meta">
            Disattivare chiude subito le sessioni della persona; i suoi diari restano sul PC e tornano con «Riattiva».
          </p>
        </section>

        <p className="esci"><a className="piccolo" href="/diari/">Torna ai diari</a></p>
      </div>
    </Fuori>
  );
}
