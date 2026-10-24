"use client";

import { useState } from "react";
import { Fuori, useSession } from "@/components/Shell";
import { ApiError, api, logout } from "@/lib/api";

export default function AccountPage() {
  const { me } = useSession({ diario: false, account: true });
  const [attuale, setAttuale] = useState("");
  const [nuova, setNuova] = useState("");
  const [ripeti, setRipeti] = useState("");
  const [esito, setEsito] = useState<{ testo: string; grave?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (nuova !== ripeti) return setEsito({ testo: "Le due password nuove non coincidono", grave: true });
    setBusy(true);
    setEsito(null);
    try {
      await api("/api/v1/auth/password", { method: "POST", json: { attuale, nuova } });
      if (me?.cambia_password) return window.location.replace("/diari/");
      setAttuale(""); setNuova(""); setRipeti("");
      setEsito({ testo: "Password cambiata. Gli altri dispositivi dovranno accedere di nuovo." });
    } catch (err) {
      setEsito({ testo: err instanceof ApiError ? err.message : "Il PC non risponde", grave: true });
    } finally {
      setBusy(false);
    }
  }

  if (!me) return <Fuori titolo="Il mio account" />;

  return (
    <Fuori titolo="Il mio account">
      <div className="foglio">
        <p className="scheda-meta">Accesso come</p>
        <p className="scheda-titolo">{me.username}</p>
        {me.amministratore && <p className="scheda-meta">Amministratore · <a href="/persone/">Persone e inviti</a></p>}

        <form className="accesso compatto" onSubmit={submit}>
          <h2>{me.cambia_password ? "Scegli la tua password" : "Cambia password"}</h2>
          {me.cambia_password && (
            <p className="avviso" role="status">
              La password che hai ricevuto è provvisoria: scegline una tua per continuare.
            </p>
          )}
          {esito && <p className={`avviso${esito.grave ? " grave" : ""}`} role="status">{esito.testo}</p>}
          <div className="campo">
            <label htmlFor="a">{me.cambia_password ? "Password provvisoria" : "Password attuale"}</label>
            <input id="a" type="password" autoComplete="current-password" value={attuale}
              onChange={(e) => setAttuale(e.target.value)} required />
          </div>
          <div className="campo">
            <label htmlFor="n">Nuova password (almeno 12 caratteri)</label>
            <input id="n" type="password" autoComplete="new-password" minLength={12} value={nuova}
              onChange={(e) => setNuova(e.target.value)} required />
          </div>
          <div className="campo">
            <label htmlFor="r">Ripeti la nuova password</label>
            <input id="r" type="password" autoComplete="new-password" minLength={12} value={ripeti}
              onChange={(e) => setRipeti(e.target.value)} required />
          </div>
          <button className="primario" type="submit" disabled={busy}>{busy ? "…" : "Salva la password"}</button>
        </form>

        <p className="esci">
          {!me.cambia_password && <a className="piccolo" href="/diari/">Torna ai diari</a>}
          <button className="piccolo" type="button"
            onClick={() => logout().finally(() => window.location.replace("/accesso/"))}>Esci</button>
        </p>
      </div>
    </Fuori>
  );
}
