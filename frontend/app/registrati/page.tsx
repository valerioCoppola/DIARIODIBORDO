"use client";

import { useState } from "react";
import { ApiError, registrati } from "@/lib/api";

export default function RegistratiPage() {
  const [codice, setCodice] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [ripeti, setRipeti] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== ripeti) return setError("Le due password non coincidono");
    setBusy(true);
    setError(null);
    try {
      await registrati(codice, username, password);
      window.location.replace("/diari/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Il PC non risponde. Controlla che il servizio sia acceso.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
    <header className="copertina">
      <h1><a href="/">Diario di Bordo</a></h1>
    </header>
    <main className="wrap">
      <form className="accesso" onSubmit={submit}>
        <p className="accesso-intro">
          Con il codice che ti è stato dato scegli il tuo nome utente e la tua password. I tuoi diari li vedi solo tu.
        </p>
        {error && <p className="avviso grave" role="alert">{error}</p>}
        <div className="campo">
          <label htmlFor="c">Codice d’invito</label>
          <input id="c" autoComplete="off" autoCapitalize="characters" spellCheck={false} value={codice}
            placeholder="ABCD-EFGH-JKLM" onChange={(e) => setCodice(e.target.value)} required />
        </div>
        <div className="campo">
          <label htmlFor="u">Nome utente</label>
          <input id="u" autoComplete="username" autoCapitalize="none" spellCheck={false} value={username}
            pattern="[a-zA-Z0-9._\-]{3,64}" title="Da 3 a 64 caratteri: lettere, cifre, punto, trattino"
            onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="campo">
          <label htmlFor="p">Password (almeno 12 caratteri)</label>
          <input id="p" type="password" autoComplete="new-password" minLength={12} value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <div className="campo">
          <label htmlFor="r">Ripeti la password</label>
          <input id="r" type="password" autoComplete="new-password" minLength={12} value={ripeti}
            onChange={(e) => setRipeti(e.target.value)} required />
        </div>
        <button className="primario" type="submit" disabled={busy}>{busy ? "Creazione…" : "Crea l’account"}</button>
        <p className="accesso-altro"><a href="/accesso/">Hai già un account? Accedi</a></p>
      </form>
    </main>
    </>
  );
}
