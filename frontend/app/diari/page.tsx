"use client";

import { useCallback, useEffect, useState } from "react";
import { HoldButton } from "@/components/HoldButton";
import { Fuori, useSession } from "@/components/Shell";
import { ApiError, api, logout } from "@/lib/api";
import { COPERTINE, apriDiario, type Copertina } from "@/lib/diario";
import { listRecordings } from "@/lib/localstore";
import { uploadRecording } from "@/lib/uploader";

type Diario = {
  id: string; nome: string; copertina: Copertina; creato_il: string; aperto_il: string | null;
  note: number; ultima_nota: string | null;
};

const giorno = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "short" });
function quando(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const oggi = new Date();
  const ieri = new Date(oggi.getFullYear(), oggi.getMonth(), oggi.getDate() - 1);
  if (d.toDateString() === oggi.toDateString()) return "oggi";
  if (d.toDateString() === ieri.toDateString()) return "ieri";
  return giorno.format(d);
}

function Libro({ d, aperto, onApri }: { d: Pick<Diario, "nome" | "copertina">; aperto?: boolean; onApri?: () => void }) {
  const dentro = (
    <>
      <span className="libro-nome">{d.nome}</span>
      <span className="libro-nastro" aria-hidden="true" />
    </>
  );
  if (!onApri) return <div className="libro-copertina" data-copertina={d.copertina}>{dentro}</div>;
  return (
    <button type="button" className="libro-copertina" data-copertina={d.copertina} data-si-apre={aperto || undefined}
      onClick={onApri} aria-label={`Apri ${d.nome}`}>
      {dentro}
    </button>
  );
}

function Modifica({ d, unico, onFatto, onAnnulla }: {
  d: Diario | null; unico: boolean; onFatto: (aperto?: Diario) => void; onAnnulla: () => void;
}) {
  const [nome, setNome] = useState(d?.nome ?? "");
  const [copertina, setCopertina] = useState<Copertina>(d?.copertina ?? "notte");
  const [conferma, setConferma] = useState("");
  const [errore, setErrore] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function salva(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrore(null);
    try {
      if (d) {
        await api(`/api/v1/diari/${d.id}`, { method: "PATCH", json: { nome, copertina } });
        onFatto();
      } else {
        onFatto(await api<Diario>("/api/v1/diari", { method: "POST", json: { nome, copertina } }));
      }
    } catch (err) {
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde");
    } finally {
      setBusy(false);
    }
  }

  async function elimina() {
    if (!d) return;
    setErrore(null);
    try {
      await api(`/api/v1/diari/${d.id}/elimina`, { method: "POST", json: { conferma } });
      onFatto();
    } catch (err) {
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde");
    }
  }

  const puoEliminare = d && conferma.trim().toLowerCase() === d.nome.toLowerCase();
  return (
    <section className="banco" aria-label={d ? `Modifica ${d.nome}` : "Nuovo diario"}>
      <form onSubmit={salva} className="banco-form">
        <div className="banco-anteprima" aria-hidden="true">
          <Libro d={{ nome: nome.trim() || (d?.nome ?? "Nuovo diario"), copertina }} />
        </div>
        <div className="banco-campi">
          <h2>{d ? "Modifica il diario" : "Nuovo diario"}</h2>
          {errore && <p className="avviso grave" role="alert">{errore}</p>}
          <div className="campo">
            <label htmlFor="nome-diario">Nome</label>
            <input id="nome-diario" value={nome} maxLength={40} required autoFocus={!d}
              placeholder="es. Lavoro, Viaggio in Islanda" onChange={(e) => setNome(e.target.value)} />
          </div>
          <fieldset className="copertine">
            <legend>Copertina</legend>
            {COPERTINE.map((c) => (
              <label key={c.id} className="tinta" data-copertina={c.id} title={c.nome}>
                <input type="radio" name="copertina" value={c.id} checked={copertina === c.id}
                  onChange={() => setCopertina(c.id)} />
                <span className="sr">{c.nome}</span>
              </label>
            ))}
          </fieldset>
          <p className="banco-comandi">
            <button className="primario" type="submit" disabled={busy || !nome.trim()}>
              {d ? "Salva" : "Crea il diario"}
            </button>
            <button className="piccolo" type="button" onClick={onAnnulla}>Annulla</button>
          </p>
        </div>
      </form>
      {d && !unico && (
        <div className="banco-elimina">
          <h3 className="mc">Eliminare il diario</h3>
          <p>
            Spariscono per sempre {d.note === 1 ? "la sua nota" : `le sue ${d.note} note`}, gli audio, la mappa
            e le cose da fare. Per confermare scrivi il nome del diario.
          </p>
          <div className="campo">
            <label htmlFor="conferma-elimina">Nome del diario</label>
            <input id="conferma-elimina" value={conferma} autoComplete="off" onChange={(e) => setConferma(e.target.value)} />
          </div>
          {puoEliminare && <HoldButton label="Elimina per sempre" hint="tieni premuto 2 s" onConfirm={elimina} />}
        </div>
      )}
    </section>
  );
}

export default function ScaffalePage() {
  const { me } = useSession({ diario: false });
  const [diari, setDiari] = useState<Diario[] | null>(null);
  const [errore, setErrore] = useState<string | null>(null);
  const [modifica, setModifica] = useState<Diario | "nuovo" | null>(null);
  const [aprendo, setAprendo] = useState<string | null>(null);
  const [inInvio, setInInvio] = useState(0);

  const carica = useCallback(async () => {
    try {
      setDiari((await api<{ diari: Diario[] }>("/api/v1/diari")).diari);
    } catch (err) {
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde. Controlla che il servizio sia acceso.");
    }
  }, []);

  useEffect(() => {
    if (!me) return;
    void carica();
    // I vocali registrati prima di chiudere un diario partono anche da qui, ognuno verso il suo diario.
    void (async () => {
      const pendenti = (await listRecordings()).filter((r) => r.status === "da_inviare" || r.status === "errore");
      setInInvio(pendenti.length);
      for (const r of pendenti) {
        await uploadRecording(r.id, () => {}).catch(() => {});
        setInInvio((n) => Math.max(0, n - 1));
      }
    })();
  }, [me, carica]);

  async function apri(d: Diario) {
    if (aprendo) return;
    setAprendo(d.id);
    const anima = new Promise((r) => setTimeout(r, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 520));
    try {
      const pagina = await apriDiario({ id: d.id, nome: d.nome, copertina: d.copertina });
      await anima;
      window.location.assign(pagina);
    } catch (err) {
      setAprendo(null);
      setErrore(err instanceof ApiError ? err.message : "Il PC non risponde");
    }
  }

  async function fatto(nuovo?: Diario) {
    setModifica(null);
    if (nuovo) return apri(nuovo);
    await carica();
  }

  if (!me) return <Fuori titolo="Diario di Bordo" />;

  return (
    <Fuori titolo="Diario di Bordo">
      <h2 className="scaffale-titolo">I tuoi diari</h2>
      {errore && <p className="avviso grave fuori-avviso" role="alert">{errore}</p>}
      {inInvio > 0 && (
        <p className="fuori-nota" role="status">
          {inInvio === 1 ? "Un vocale in invio al PC…" : `${inInvio} vocali in invio al PC…`}
        </p>
      )}

      {modifica && (
        <Modifica d={modifica === "nuovo" ? null : modifica} unico={(diari?.length ?? 0) <= 1}
          onFatto={fatto} onAnnulla={() => setModifica(null)} />
      )}

      {diari === null ? (
        <p className="fuori-nota">Caricamento…</p>
      ) : (
        <ul className="libri" aria-label="Diari">
          {diari.map((d) => (
            <li key={d.id} className="libro">
              <div className="libro-pagine">
                <Libro d={d} aperto={aprendo === d.id} onApri={() => void apri(d)} />
              </div>
              <p className="libro-meta">
                {d.note === 0 ? "vuoto" : `${d.note} ${d.note === 1 ? "nota" : "note"}`}
                {d.ultima_nota ? ` · ${quando(d.ultima_nota)}` : ""}
              </p>
              <button className="libro-modifica" type="button" onClick={() => setModifica(d)}
                aria-label={`Modifica ${d.nome}`}>modifica</button>
            </li>
          ))}
          <li className="libro">
            <button type="button" className="libro-nuovo" onClick={() => setModifica("nuovo")}>
              <span aria-hidden="true">+</span>
              Nuovo diario
            </button>
          </li>
        </ul>
      )}

      <p className="fuori-nota scaffale-aiuto">
        Ogni diario si riapre alla pagina in cui l’hai chiuso. Per chiuderlo tira giù il nastro in cima alla pagina.
      </p>

      <nav className="fuori-piede" aria-label="Account">
        <span className="mc">{me.username}</span>
        <a href="/account/">Il mio account</a>
        {me.amministratore && <a href="/persone/">Persone</a>}
        <button className="collegamento" type="button"
          onClick={() => logout().finally(() => window.location.replace("/accesso/"))}>Esci</button>
      </nav>
    </Fuori>
  );
}
